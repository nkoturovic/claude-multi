package watcher

// Observations, not product assertions. The Python runner requires each
// scenario's controls and records the outcome; a reproduced defect is not fixed
// or described as a product pass. Run only inside bwrap --unshare-net.
import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
)

func gwtestWatcher(t *testing.T) (*Watcher, *config.Config, chan int) {
	t.Helper()
	root := t.TempDir()
	authDir := filepath.Join(root, "auth")
	if err := os.Mkdir(authDir, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "config.yaml")
	text := fmt.Sprintf("host: 127.0.0.1\nport: 49153\nauth-dir: %q\napi-keys: [dummy-gwtest-client]\ndiscovery: {enabled: false}\n", authDir)
	if err := os.WriteFile(path, []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	calls := make(chan int, 16)
	w, err := NewWatcher(path, authDir, func(c *config.Config) { calls <- len(c.APIKeys) })
	if err != nil {
		t.Fatal(err)
	}
	w.SetConfig(cfg)
	t.Cleanup(func() { _ = w.Stop() })
	return w, cfg, calls
}

func gwtestObservation(t *testing.T, row map[string]any) {
	t.Helper()
	data, err := json.Marshal(row)
	if err != nil {
		t.Fatal(err)
	}
	fmt.Printf("GWTEST_REPRO=%s\n", data)
}

func TestGatewayB020MutableGuard(t *testing.T) {
	w, cfg, calls := gwtestWatcher(t)
	// Exercise the same reload transaction used by the fsnotify timer, but
	// synchronously: this is a mutable-pointer reproducer, not a timing race.
	text := fmt.Sprintf("host: 127.0.0.1\nport: 49154\nauth-dir: %q\napi-keys: []\ndiscovery: {enabled: false}\n", cfg.AuthDir)
	if err := os.WriteFile(w.configPath, []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
	w.ReloadConfigIfChanged()
	select {
	case <-calls:
		t.Fatal("intact prior-config control failed: key drop was not refused")
	default:
	}
	// The old snapshot still contains the key. Only mutate the live pointer,
	// under its normal lock; no production mutation route is bypassed by the
	// HTTP companion scenario (it separately checks the management allowlist).
	w.clientsMutex.Lock()
	cfg.APIKeys = nil
	w.clientsMutex.Unlock()
	w.ReloadConfigIfChanged()
	accepted := false
	select {
	case count := <-calls:
		if count != 0 {
			t.Fatal("unexpected reload key count")
		}
		accepted = true
	default:
	}
	gwtestObservation(t, map[string]any{"row": "B020", "intact_guard_refused": true,
		"in_place_mutation": true, "key_drop_accepted": accepted})
}

func TestGatewayB024TimerAfterStop(t *testing.T) {
	w, cfg, calls := gwtestWatcher(t)
	text := fmt.Sprintf("host: 127.0.0.1\nport: 49154\nauth-dir: %q\napi-keys: [dummy-gwtest-next]\ndiscovery: {enabled: false}\n", cfg.AuthDir)
	if err := os.WriteFile(w.configPath, []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
	// A real AfterFunc callback expires and clears configReloadTimer, then
	// waits on the transaction lock. Stop cannot cancel an already-fired timer.
	// This imposes an existing interleaving, without altering production code.
	w.configApplyMu.Lock()
	locked := true
	defer func() {
		if locked {
			w.configApplyMu.Unlock()
		}
	}()
	w.scheduleConfigReload()
	deadline := time.Now().Add(3 * time.Second)
	for {
		w.configReloadMu.Lock()
		fired := w.configReloadTimer == nil
		w.configReloadMu.Unlock()
		if fired {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("reload timer did not enter its callback")
		}
		time.Sleep(time.Millisecond)
	}
	if err := w.Stop(); err != nil {
		t.Fatal(err)
	}
	if !w.stopped.Load() {
		t.Fatal("Stop did not mark watcher stopped")
	}
	w.configApplyMu.Unlock()
	locked = false
	applied := false
	select {
	case count := <-calls:
		if count != 1 {
			t.Fatal("unexpected callback payload")
		}
		applied = true
	case <-time.After(500 * time.Millisecond):
	}
	gwtestObservation(t, map[string]any{"row": "B024", "timer_entered": true,
		"stop_returned_before_release": true, "callback_after_stop": applied,
		"observation_bound_ms": 500})
}
