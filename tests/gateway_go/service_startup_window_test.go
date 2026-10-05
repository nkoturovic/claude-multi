package cliproxy

// Hold the real pre-watcher startup hook while the OUTSIDE Python fixture
// atomically replaces config.yaml. The service retains a read-only policy
// directory. No fake watcher, plugin host or auth manager replaces production.
import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
)

func TestGatewayB017StartupWindow(t *testing.T) {
	path, control := os.Getenv("GWTEST_CONFIG"), os.Getenv("GWTEST_CONTROL")
	if !filepath.IsAbs(path) || !filepath.IsAbs(control) {
		t.Fatal("explicit fixture config/control paths required")
	}
	cfg, err := config.LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	wait := func(name string) bool {
		for {
			if _, err := os.Stat(filepath.Join(control, name)); err == nil {
				return true
			}
			select {
			case <-ctx.Done():
				return false
			case <-time.After(5 * time.Millisecond):
			}
		}
	}
	hookReached := false
	service, err := NewBuilder().WithConfig(cfg).WithConfigPath(path).WithHooks(Hooks{
		OnAfterStart: func(*Service) {
			hookReached = true
			if err := os.WriteFile(filepath.Join(control, "paused"), []byte("pre-watcher\n"), 0600); err != nil {
				t.Error(err)
				cancel()
				return
			}
			if !wait("release") {
				t.Error("startup replacement was not released")
			}
		},
	}).Build()
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		if wait("done") {
			cancel()
		}
	}()
	err = service.Run(ctx)
	if !hookReached {
		t.Fatal("pre-watcher startup hook was not reached")
	}
	if err != nil && !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(control, "done")); err != nil {
		t.Fatal("Python controller did not complete observations")
	}
}
