package cliproxy

// This overlay deliberately uses only pre-patch Service/Builder seams. It
// must compile with any ONE local patch omitted, especially the startup gate.
// The Python caller runs this executable only in bwrap --unshare-net.
import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
)

func TestGatewayStartupFileAuth(t *testing.T) {
	root := t.TempDir()
	t.Setenv("WRITABLE_PATH", root)
	authDir := filepath.Join(root, "auth")
	if err := os.Mkdir(authDir, 0700); err != nil {
		t.Fatal(err)
	}
	// No refresh token; no real identity. Select a wire structurally, not by ID.
	if err := os.WriteFile(filepath.Join(authDir, "synthetic.json"), []byte(`{"type":"claude","email":"gwtest@example.invalid","access_token":"dummy-gwtest-access","expired":"2099-01-01T00:00:00Z"}`), 0600); err != nil {
		t.Fatal(err)
	}
	models := registry.GetClaudeModels()
	if len(models) == 0 {
		t.Fatal("no embedded Claude model for synthetic auth")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	listener.Close()
	path := filepath.Join(root, "config.yaml")
	text := fmt.Sprintf("host: 127.0.0.1\nport: %d\nauth-dir: %q\napi-keys: [dummy-gwtest-client]\ndiscovery: {enabled: false}\nremote-management: {disable-control-panel: true}\noauth-model-alias:\n  claude:\n    - name: %q\n      alias: gwtest-startup-auth\n      fork: true\n", port, authDir, models[0].ID)
	if err := os.WriteFile(path, []byte(text), 0600); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}}
	get := func(route string) (int, string, error) {
		req, _ := http.NewRequest("GET", fmt.Sprintf("http://127.0.0.1:%d%s", port, route), nil)
		req.Header.Set("Authorization", "Bearer dummy-gwtest-client")
		resp, err := client.Do(req)
		if err != nil {
			return 0, "", err
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(resp.Body)
		return resp.StatusCode, string(body), err
	}
	// OnAfterStart precedes watcher creation on both pristine and patched code.
	// Holding it lets the first model request race initial file-auth registration
	// without relying on the omitted patch's event hooks or test helpers.
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	service, err := NewBuilder().WithConfig(cfg).WithConfigPath(path).WithHooks(Hooks{
		OnAfterStart: func(*Service) { close(started); <-release },
	}).Build()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- service.Run(ctx) }()
	t.Cleanup(func() {
		unblock()
		cancel()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Error("service failed to stop")
		}
	})
	select {
	case <-started:
	case <-time.After(10 * time.Second):
		t.Fatal("OnAfterStart not reached")
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		if status, _, err := get("/healthz"); err == nil && status == 200 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("health listener unavailable")
		}
		time.Sleep(5 * time.Millisecond)
	}
	type response struct { status int; body string; err error }
	answer := make(chan response, 1)
	go func() { status, body, err := get("/v1/models"); answer <- response{status, body, err} }()
	select {
	case got := <-answer:
		t.Fatalf("model route escaped initial file-auth gate: status=%d error=%v", got.status, got.err)
	case <-time.After(200 * time.Millisecond):
	}
	// Health must remain independent while the model route is held.
	if status, _, err := get("/healthz"); err != nil || status != 200 {
		t.Fatalf("health held by model gate: status=%d error=%v", status, err)
	}
	unblock()
	select {
	case got := <-answer:
		if got.err != nil || got.status != 200 || !strings.Contains(got.body, `"gwtest-startup-auth"`) {
			t.Fatalf("first model reply lacks synthetic file-auth alias: status=%d error=%v", got.status, got.err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("model route failed to open after initial auth load")
	}
}
