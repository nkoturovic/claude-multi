package cliproxy

// The independent probe calls only APIs present before the admitted series.
import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
