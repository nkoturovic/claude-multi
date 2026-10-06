package cliproxy

// The independent probe calls only APIs present before the admitted series.
import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	sdkauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/auth"
	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	log "github.com/sirupsen/logrus"
)

func TestOmissionCredentialReceipt(t *testing.T) {
	store := sdkauth.NewFileTokenStore()
	root := t.TempDir()
	store.SetBaseDir(root)
	manager := coreauth.NewManager(store, nil, nil)
	var buf bytes.Buffer
	oldOut, oldLevel := log.StandardLogger().Out, log.GetLevel()
	log.SetOutput(&buf)
	log.SetLevel(log.InfoLevel)
	t.Cleanup(func() { log.SetOutput(oldOut); log.SetLevel(oldLevel) })
	auth := &coreauth.Auth{ID: "omission-save.json", FileName: "omission-save.json", Provider: "claude", Metadata: map[string]any{"type": "claude", "access_token": "dummy-omission"}}
	registered, err := manager.Register(context.Background(), auth)
	if err != nil {
		t.Fatal(err)
	}
	registered.Metadata["access_token"] = "dummy-omission-new"
	if _, err = manager.Update(context.Background(), registered); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(filepath.Join(root, "omission-save.json")); err != nil {
		t.Fatal("credential file missing")
	}
	if !strings.Contains(buf.String(), "credential_save_v1 operation=update result=persisted") {
		t.Fatal("durable update receipt missing")
	}
}

func TestOmissionAuthSnapshot(t *testing.T) {
	original := &coreauth.Auth{Metadata: map[string]any{"nested": []any{map[string]any{"value": "before"}}}, Attributes: map[string]string{}}
	clone := original.Clone()
	clone.Attributes["new"] = "value"
	clone.Metadata["nested"].([]any)[0].(map[string]any)["value"] = "after"
	if len(original.Attributes) != 0 || original.Metadata["nested"].([]any)[0].(map[string]any)["value"] != "before" {
		t.Fatal("clone aliases caller-owned containers")
	}
}

// This probe deliberately uses only the original Store/ProviderExecutor and
// Service.Shutdown APIs. It compiles without the refresh-join patch or its tests.
type omissionShutdownExecutor struct {
	coreauth.ProviderExecutor
	entered   chan struct{}
	cancelled chan struct{}
	release   <-chan struct{}
}

func (*omissionShutdownExecutor) Identifier() string { return "omission-shutdown" }
func (e *omissionShutdownExecutor) Refresh(ctx context.Context, a *coreauth.Auth) (*coreauth.Auth, error) {
	close(e.entered)
	<-ctx.Done()
	close(e.cancelled)
	<-e.release // an already-started, cancellation-shielded refresh still succeeds
	a = a.Clone()
	a.Metadata["refresh_token"] = "omission-replacement-refresh"
	return a, nil
}

type omissionShutdownStore struct {
	coreauth.Store
	entered chan struct{}
	release <-chan struct{}
	saved   chan string
}

func (s *omissionShutdownStore) Save(_ context.Context, a *coreauth.Auth) (string, error) {
	close(s.entered)
	<-s.release
	token, _ := a.Metadata["refresh_token"].(string)
	s.saved <- token
	return "", nil
}

func omissionShutdownRelease() (chan struct{}, func()) {
	ch := make(chan struct{})
	var once sync.Once
	return ch, func() { once.Do(func() { close(ch) }) }
}

func TestOmissionRefreshShutdown(t *testing.T) {
	executorHold, releaseExecutor := omissionShutdownRelease()
	storeHold, releaseStore := omissionShutdownRelease()
	exec := &omissionShutdownExecutor{entered: make(chan struct{}), cancelled: make(chan struct{}), release: executorHold}
	store := &omissionShutdownStore{entered: make(chan struct{}), release: storeHold, saved: make(chan string, 1)}
	manager := coreauth.NewManager(store, nil, nil)
	manager.RegisterExecutor(exec)
	_, err := manager.Register(coreauth.WithSkipPersist(context.Background()), &coreauth.Auth{
		ID: "omission-shutdown", Provider: "omission-shutdown",
		Metadata: map[string]any{"refresh_token": "omission-old-refresh", "refresh_interval_seconds": 3600},
	})
	if err != nil {
		t.Fatal(err)
	}
	wait := func(ch <-chan struct{}) {
		t.Helper()
		select {
		case <-ch:
		case <-time.After(5 * time.Second):
			t.Fatal("shutdown probe did not reach its barrier")
		}
	}
	manager.StartAutoRefresh(context.Background(), time.Hour)
	defer manager.StopAutoRefresh()
	defer releaseStore()
	defer releaseExecutor()
	wait(exec.entered)
	service := &Service{coreManager: manager}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- service.Shutdown(ctx) }()
	wait(exec.cancelled)
	// Record early exits without abandoning the fixture: release and observe the
	// replacement save even in the patch-omitted, behaviourally red control.
	returned := false
	checkBlocked := func() {
		t.Helper()
		if returned {
			return
		}
		select {
		case err := <-done:
			returned = true
			t.Errorf("Shutdown returned before replacement credential save: %v", err)
		case <-time.After(20 * time.Millisecond):
		}
	}
	checkBlocked()
	releaseExecutor()
	wait(store.entered)
	checkBlocked()
	releaseStore()
	if !returned {
		select {
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("Shutdown did not finish after replacement save")
		}
	}
	select {
	case token := <-store.saved:
		if token != "omission-replacement-refresh" {
			t.Fatalf("saved token = %q, want replacement refresh token", token)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("replacement token was not saved")
	}
}

func TestOmissionOverlay(t *testing.T) {
	// Both loaders must refuse an unknown overlay field; pristine ignores it.
	data := []byte("oauth-extra-models:\n  claude:\n    - id: omission-overlay-probe\n      unexpected: true\n")
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := config.LoadConfig(path); err == nil {
		t.Fatal("invalid file overlay accepted")
	}
	if _, err := config.ParseConfigBytes(data); err == nil {
		t.Fatal("invalid payload overlay accepted")
	}
}
