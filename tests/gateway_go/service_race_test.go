package cliproxy

// The race pairs and shared credential metadata over manager/HTTP, SERVICE
// label. The production builder path (NewBuilder().WithConfig()
// .WithConfigPath(): the normal plugin host, the real watcher, auth manager,
// executors and HTTP server) runs the rendered
// fixture config. The OUTSIDE Python driver sends real HTTP traffic through
// the ingress bridge, performs accepted reloads and auth-file updates, and
// serves the fake upstream through the egress bridge. Observation only.
//
// Driver handshakes (control-directory files; b108-armed answers b108-arm with
// {"armed": n, "aliases": [...]}):
//   - b108-arm, config-credential case (GWTEST_B108_MODE=config): config-
//     synthesized claude-api-key credentials carrying a synthetic
//     "sk-ant-oat-gwtest" token (their Metadata is empty, so nil) are marked
//     auth_kind=setup_token through the manager's public Update. Everything
//     after that is the ordinary manager/HTTP path.
//   - b108-arm, file-auth case (GWTEST_B108_MODE=file): synthetic file-backed
//     Claude setup-token credentials written by the driver are loaded by the
//     normal file synthesizer and are NOT modified: their Metadata is the auth
//     document, cloned and persisted by the manager as usual. File-backed
//     Claude credentials have no base URL, so their requests name
//     https://api.anthropic.com; the one test-local surgery is a manager
//     RoundTripperProvider that answers ONLY those credentials' requests to that
//     host from the local fake upstream (GWTEST_FILE_UPSTREAM, loopback inside
//     the unshared network namespace) and refuses every other host. Each
//     credential's routable model id is read from the model registry (its own
//     prefixed registration), never pinned.
import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
)

const gwtestFileAuthPrefix = "gwtest-b108-file-"

func gwtestFileAuth(auth *coreauth.Auth) bool {
	return auth != nil && auth.Provider == "claude" && strings.HasPrefix(auth.FileName, gwtestFileAuthPrefix) &&
		auth.Attributes[coreauth.AttributeSourceBackend] == coreauth.AuthSourceFile
}

type gwtestFileUpstream struct {
	base     coreauth.RoundTripperProvider
	upstream *url.URL
	next     http.RoundTripper
	answered atomic.Int64
	refused  atomic.Int64
}

func (p *gwtestFileUpstream) RoundTripperFor(auth *coreauth.Auth) http.RoundTripper {
	if gwtestFileAuth(auth) {
		return p
	}
	return p.base.RoundTripperFor(auth)
}

func (p *gwtestFileUpstream) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.URL.Scheme != "https" || req.URL.Host != "api.anthropic.com" {
		p.refused.Add(1)
		return nil, fmt.Errorf("gwtest: file-auth request to an unexpected host refused")
	}
	local := req.Clone(req.Context())
	target := *p.upstream
	target.Path = strings.TrimRight(p.upstream.Path, "/") + req.URL.Path
	target.RawQuery = req.URL.RawQuery
	local.URL, local.Host = &target, target.Host
	p.answered.Add(1)
	return p.next.RoundTrip(local)
}

func TestGatewayRaceService(t *testing.T) {
	path, control, scenario := os.Getenv("GWTEST_CONFIG"), os.Getenv("GWTEST_CONTROL"), os.Getenv("GWTEST_SCENARIO")
	if !filepath.IsAbs(path) || !filepath.IsAbs(control) || scenario == "" {
		t.Fatal("explicit fixture config/control paths and scenario required")
	}
	mode := os.Getenv("GWTEST_B108_MODE")
	exists := func(name string) bool {
		_, err := os.Stat(filepath.Join(control, name))
		return err == nil
	}
	write := func(name, text string) { // complete or absent: write, then rename
		staged := filepath.Join(control, "."+name+".tmp")
		if err := os.WriteFile(staged, []byte(text), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(staged, filepath.Join(control, name)); err != nil {
			t.Fatal(err)
		}
	}
	cfg, err := config.LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	service, err := NewBuilder().WithConfig(cfg).WithConfigPath(path).Build()
	if err != nil {
		t.Fatal(err)
	}
	var fileUpstream *gwtestFileUpstream
	if mode == "file" {
		upstream, errURL := url.Parse(os.Getenv("GWTEST_FILE_UPSTREAM"))
		if errURL != nil || upstream.Scheme != "http" || upstream.Hostname() != "127.0.0.1" {
			t.Fatal("file-auth mode needs a loopback GWTEST_FILE_UPSTREAM")
		}
		fileUpstream = &gwtestFileUpstream{base: newDefaultRoundTripperProvider(), upstream: upstream,
			next: &http.Transport{Proxy: nil, ForceAttemptHTTP2: false}}
		service.coreManager.SetRoundTripperProvider(fileUpstream)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- service.Run(ctx) }()
	armed := -1
	for !exists("done") {
		select {
		case err := <-done:
			t.Fatalf("service stopped before the driver finished: %v", err)
		case <-ctx.Done():
			t.Fatal("scenario bound exceeded")
		case <-time.After(5 * time.Millisecond):
		}
		if armed < 0 && exists("b108-arm") {
			armed = 0
			aliases := []string{}
			for _, auth := range service.coreManager.List() {
				switch mode {
				case "file":
					// Observed, never modified: file metadata present, token OAuth-shaped.
					if !gwtestFileAuth(auth) || len(auth.Metadata) == 0 || auth.Prefix == "" ||
						!strings.HasPrefix(fmt.Sprint(auth.Metadata["access_token"]), "sk-ant-oat-gwtest") {
						continue
					}
					routable := []string{}
					for _, model := range registry.GetGlobalRegistry().GetModelsForClient(auth.ID) {
						if model != nil && strings.HasPrefix(model.ID, auth.Prefix+"/") {
							routable = append(routable, model.ID)
						}
					}
					if len(routable) == 0 {
						continue
					}
					sort.Strings(routable)
					aliases = append(aliases, routable[0])
					armed++
				case "config":
					if auth == nil || auth.Provider != "claude" || !strings.HasPrefix(auth.Attributes["api_key"], "sk-ant-oat-gwtest") {
						continue
					}
					marked := auth.Clone()
					marked.Attributes["auth_kind"] = "setup_token"
					if _, err := service.coreManager.Update(context.Background(), marked); err != nil {
						t.Fatal(err)
					}
					armed++
				default:
					t.Fatal("b108-arm needs GWTEST_B108_MODE=file or config")
				}
			}
			answer, _ := json.Marshal(map[string]any{"armed": armed, "aliases": aliases})
			write("b108-armed", string(answer)+"\n")
		}
	}
	// Preparation proof, read after all traffic: the credentials the manager
	// now holds carry the identity request preparation stores.
	prepared := 0
	if mode == "file" {
		for _, auth := range service.coreManager.List() {
			if uuid, _ := auth.Metadata["account_uuid"].(string); gwtestFileAuth(auth) && uuid != "" {
				prepared++
			}
		}
	}
	cancel()
	select {
	case err := <-done:
		if err != nil && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal(err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("service failed to stop")
	}
	result := map[string]any{"scenario": scenario, "b108_armed": armed}
	if fileUpstream != nil {
		result["file_prepared"] = prepared
		result["file_upstream_answered"] = fileUpstream.answered.Load()
		result["file_upstream_refused"] = fileUpstream.refused.Load()
	}
	encoded, _ := json.Marshal(result)
	fmt.Println("GWTEST_RACE_RESULT=" + string(encoded))
}
