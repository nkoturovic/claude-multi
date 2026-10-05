package cliproxy

// Independent of patch #18: copied into every omission build by the diagnostic
// derivation. Use only APIs that predate #18, so omitting #18 still builds and goes red.
import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	runtimeexecutor "github.com/router-for-me/CLIProxyAPI/v7/internal/runtime/executor"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
	"github.com/tidwall/gjson"
)

func TestOmissionCodexIdentity(t *testing.T) {
	captured := make(chan http.Header, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		captured <- r.Header.Clone()
		_, _ = io.Copy(io.Discard, r.Body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"id":"codexprobe","object":"response.compaction","output":[]}`)
	}))
	defer server.Close()
	cfg := &config.Config{}
	auth := &cliproxyauth.Auth{Provider: "codex", Attributes: map[string]string{"base_url": server.URL}, Metadata: map[string]any{"access_token": "fixture-only"}}
	_, err := runtimeexecutor.NewCodexExecutor(cfg).Execute(context.Background(), auth,
		cliproxyexecutor.Request{Model: "codexprobe-omission", Payload: []byte(`{"model":"codexprobe-omission","input":[]}`)},
		cliproxyexecutor.Options{SourceFormat: sdktranslator.FormatOpenAIResponse, Alt: "responses/compact"})
	if err != nil {
		t.Fatalf("fixture compact request: %v", err)
	}
	for path, h := range map[string]http.Header{
		"final-http": <-captured,
	} {
		for key, want := range map[string]string{
			"User-Agent": "codex-tui/0.159.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.159.1)",
			"Originator": "codex-tui", "Version": "0.159.1",
		} {
			if got := h.Get(key); got != want {
				t.Errorf("%s %s = %q, want %q", path, key, got, want)
			}
		}
	}
}

// The probe consumes Python's actual T1 render, never a second hand-written Sol
// capability table. Dummy OAuth auths use a test-local base_url at the executor
// seam (the shipped OAuth executor otherwise targets a fixed HTTPS origin).
// This is the same service/manager/registry path as the overlay postCheck tests.
type codexProbeOverlaySpec struct {
	Wire     string            `json:"wire"`
	Aliases  map[string]string `json:"aliases"`
	Levels   bool              `json:"levels"`
	Channels map[string][]struct {
		Name     string `json:"name"`
		Context  int    `json:"max-context-length"`
		Output   int    `json:"max-completion-tokens"`
		Thinking struct {
			Levels []string `json:"levels"`
		} `json:"thinking"`
	} `json:"channels"`
}

func codexProbeLoadFixture(t *testing.T) (*config.Config, codexProbeOverlaySpec) {
	t.Helper()
	root := os.Getenv("CODEX_PROBE_FIXTURE_DIR")
	if root == "" {
		t.Fatal("CODEX_PROBE_FIXTURE_DIR required; no empty test selection")
	}
	path := filepath.Join(root, "config.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := config.LoadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := config.ParseConfigBytes(data)
	if err != nil {
		t.Fatal(err)
	}
	// Both public loaders must preserve exactly the same capability/alias/payload
	// document; no dependence on #16 Go types, so its RC omission still compiles.
	if !reflect.DeepEqual(cfg, parsed) {
		t.Fatal("file/payload config loaders differ")
	}
	specBytes, err := os.ReadFile(filepath.Join(root, "spec.json"))
	if err != nil {
		t.Fatal(err)
	}
	var spec codexProbeOverlaySpec
	if err = json.Unmarshal(specBytes, &spec); err != nil {
		t.Fatal(err)
	}
	if len(spec.Aliases) != 5 || len(spec.Channels) != 2 {
		t.Fatal("incomplete capability fixture")
	}
	return cfg, spec
}

func codexProbePipeline(t *testing.T, cfg *config.Config, channel, baseURL string) *cliproxyauth.Manager {
	t.Helper()
	id := "codexprobe-" + channel
	auth := &cliproxyauth.Auth{ID: id, Provider: channel, Status: cliproxyauth.StatusActive,
		Attributes: map[string]string{"auth_kind": "oauth", "plan_type": "pro", "base_url": baseURL},
		Metadata:   map[string]any{"type": channel, "access_token": "codexprobe-fixture", "account_id": "codexprobe-account"}}
	manager := cliproxyauth.NewManager(nil, nil, nil)
	manager.SetConfig(cfg)
	manager.SetOAuthModelAlias(cfg.OAuthModelAlias)
	if channel == "codex" {
		manager.RegisterExecutor(runtimeexecutor.NewCodexExecutor(cfg))
	}
	if _, err := manager.Register(context.Background(), auth); err != nil {
		t.Fatal(err)
	}
	reg := GlobalModelRegistry()
	reg.UnregisterClient(id)
	t.Cleanup(func() { reg.UnregisterClient(id) })
	service := &Service{cfg: cfg, coreManager: manager}
	service.completeModelRegistrationForAuth(context.Background(), auth)
	return manager
}

func TestCodexProbeOverlayLoaderParity(t *testing.T) {
	cfg, spec := codexProbeLoadFixture(t)
	for channel, expected := range spec.Channels {
		codexProbePipeline(t, cfg, channel, "http://127.0.0.1:1") // registration only, no request
		models := GlobalModelRegistry().GetModelsForClient("codexprobe-" + channel)
		for _, row := range expected {
			count := 0
			for _, model := range models {
				if model.ID == row.Name || model.MetadataModelID == row.Name {
					count++
					if model.ContextLength != row.Context || model.MaxCompletionTokens != row.Output {
						t.Fatalf("%s capability limits lost", channel)
					}
					if len(row.Thinking.Levels) > 0 {
						if model.Thinking == nil || !reflect.DeepEqual(model.Thinking.Levels, row.Thinking.Levels) {
							t.Fatalf("%s levels lost", channel)
						}
					} else if model.Thinking != nil {
						t.Fatalf("%s invented thinking", channel)
					}
				}
			}
			if count == 0 {
				t.Fatalf("%s overlay registration absent", channel)
			}
		}
	}
}

func TestCodexProbeSolEfforts(t *testing.T) {
	cfg, spec := codexProbeLoadFixture(t)
	captured := make(chan []byte, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		captured <- body
		if r.URL.Path != "/responses" || r.Header.Get("Authorization") != "Bearer codexprobe-fixture" {
			t.Error("wrong route or credential")
		}
		if r.Header.Get("Version") != "0.159.1" {
			t.Error("a gateway without the Codex client identity")
		}
		output := `[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}]`
		if strings.Contains(string(body), "codexprobe_tool_setup") && !strings.Contains(string(body), "function_call_output") {
			output = `[{"type":"function_call","id":"fc_codexprobe","call_id":"call_codexprobe","name":"codexprobe_tool","arguments":"{}","status":"completed"}]`
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = fmt.Fprintf(w, "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_codexprobe\",\"model\":%q,\"status\":\"completed\",\"output\":%s,\"usage\":{\"input_tokens\":1,\"output_tokens\":1,\"total_tokens\":2}}}\n\n", spec.Wire, output)
	}))
	defer server.Close()
	manager := codexProbePipeline(t, cfg, "codex", server.URL)
	for effort, alias := range spec.Aliases {
		// The setup is separately counted, and the continuation is built from its
		// returned function call (not an invented pre-existing provider response).
		for _, mode := range []string{"http", "stream", "tool"} {
			t.Run(effort+"/"+mode, func(t *testing.T) {
				input := []any{map[string]any{"role": "user", "content": "fixture"}}
				send := func(stream bool, items []any) []byte {
					t.Helper()
					payload, _ := json.Marshal(map[string]any{"model": alias, "input": items,
						"reasoning": map[string]any{"effort": effort, "summary": "concise"}, "include": []string{"reasoning.encrypted_content"},
						"tools": []any{map[string]any{"type": "function", "name": "codexprobe_tool", "parameters": map[string]any{"type": "object", "properties": map[string]any{}}}}})
					ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
					defer cancel()
					req := cliproxyexecutor.Request{Model: alias, Payload: payload}
					opts := cliproxyexecutor.Options{SourceFormat: sdktranslator.FormatOpenAIResponse, Stream: stream}
					var response []byte
					if stream {
						res, err := manager.ExecuteStream(ctx, []string{"codex"}, req, opts)
						if err != nil {
							t.Fatal(err)
						}
						for chunk := range res.Chunks {
							if chunk.Err != nil {
								t.Fatal(chunk.Err)
							}
							response = append(response, chunk.Payload...)
						}
						if !strings.Contains(string(response), "response.completed") {
							t.Fatal("stream completion absent")
						}
					} else {
						res, err := manager.Execute(ctx, []string{"codex"}, req, opts)
						if err != nil {
							t.Fatal(err)
						}
						response = res.Payload
						if !gjson.GetBytes(response, "output").IsArray() {
							t.Fatal("HTTP completion absent")
						}
					}
					var upstream []byte
					select {
					case upstream = <-captured:
					default:
						t.Fatal("no upstream capture")
					}
					if gjson.GetBytes(upstream, "model").String() != spec.Wire {
						t.Fatal("alias retargeted")
					}
					if spec.Levels && gjson.GetBytes(upstream, "reasoning.effort").String() != effort {
						t.Fatal("effort lost")
					}
					summary := gjson.GetBytes(upstream, "reasoning.summary")
					if spec.Levels {
						if summary.String() != "concise" {
							t.Fatal("source summary lost")
						}
					} else if summary.Exists() {
						t.Fatal("no-level negative control retained summary")
					}
					if gjson.GetBytes(upstream, "include").Raw != `["reasoning.encrypted_content"]` {
						t.Fatal("encrypted include lost")
					}
					return response
				}
				if mode == "tool" {
					setup := []any{map[string]any{"role": "user", "content": "codexprobe_tool_setup"}}
					response := send(false, setup)
					call := gjson.GetBytes(response, "output.0")
					if call.Get("type").String() != "function_call" {
						t.Fatal("setup did not return tool call")
					}
					var item any
					if err := json.Unmarshal([]byte(call.Raw), &item); err != nil {
						t.Fatal(err)
					}
					input = append(setup, item, map[string]any{"type": "function_call_output", "call_id": call.Get("call_id").String(), "output": "fixture result"})
				}
				send(mode == "stream", input)
			})
		}
	}
}
