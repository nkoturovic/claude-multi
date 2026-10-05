package executor

// Independent omission probes: only APIs that predate the admitted series, no
// patch-provided helpers.
// Executed by Python inside bwrap --unshare-net, never against operator state.
import (
	"context"
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	claudeauth "github.com/router-for-me/CLIProxyAPI/v7/internal/auth/claude"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	coreauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
	coreexecutor "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/executor"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
)

type probeOmissionTransport func(*http.Request) (*http.Response, error)

func (f probeOmissionTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func probeOmissionResponse(r *http.Request, status int, body string) *http.Response {
	return &http.Response{StatusCode: status, Header: http.Header{}, Body: io.NopCloser(strings.NewReader(body)), Request: r}
}
func probeOmissionCompat(t *testing.T, transport http.RoundTripper) error {
	t.Helper()
	ctx := context.WithValue(context.Background(), "cliproxy.roundtripper", transport)
	auth := &coreauth.Auth{ID: "omission-auth", Provider: "openai-compatibility", Attributes: map[string]string{"api_key": "dummy-omission", "base_url": "https://omission.invalid/v1"}}
	payload := []byte(`{"model":"omission-probe","messages":[{"role":"user","content":"hi"}],"prompt_cache_retention":"24h"}`)
	_, err := NewOpenAICompatExecutor("openai-compatibility", &config.Config{}).Execute(ctx, auth, coreexecutor.Request{Model: "omission-probe", Payload: payload}, coreexecutor.Options{SourceFormat: translator.FormatOpenAI, OriginalRequest: payload})
	return err
}
func TestOmissionCompatRetention(t *testing.T) {
	hits := 0
	err := probeOmissionCompat(t, probeOmissionTransport(func(r *http.Request) (*http.Response, error) {
		hits++
		body, _ := io.ReadAll(r.Body)
		if strings.Contains(string(body), `"prompt_cache_retention"`) {
			t.Error("retention escaped final compat boundary")
		}
		return probeOmissionResponse(r, 200, `{"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}`), nil
	}))
	if err != nil || hits != 1 {
		t.Fatalf("request did not complete: hits=%d err=%v", hits, err)
	}
}
func TestOmissionKeyedSafety(t *testing.T) {
	hits := 0
	err := probeOmissionCompat(t, probeOmissionTransport(func(r *http.Request) (*http.Response, error) {
		hits++
		return probeOmissionResponse(r, 401, `{"error":{"message":"omission-secret-echo-canary"}}`), nil
	}))
	if hits != 1 || err == nil {
		t.Fatal("expected one failed upstream request")
	}
	if strings.Contains(err.Error(), "omission-secret-echo-canary") {
		t.Fatal("untrusted provider error escaped")
	}
}
func TestOmissionCredentialedRedirect(t *testing.T) {
	var hits atomic.Int32
	ctx := context.WithValue(context.Background(), "cliproxy.roundtripper", probeOmissionTransport(func(r *http.Request) (*http.Response, error) {
		hits.Add(1)
		if r.URL.Host == "omission.invalid" {
			reply := probeOmissionResponse(r, 302, "")
			reply.Header.Set("Location", "https://other.omission.invalid/escaped")
			return reply, nil
		}
		return probeOmissionResponse(r, 200, "escaped"), nil
	}))
	request, _ := http.NewRequest("GET", "https://omission.invalid/probe", nil)
	reply, err := NewClaudeExecutor(&config.Config{}).HttpRequest(ctx, &coreauth.Auth{Attributes: map[string]string{"api_key": "dummy-omission"}}, request)
	if reply != nil && reply.Body != nil {
		reply.Body.Close()
	}
	if err != nil || reply == nil || reply.StatusCode != 302 || reply.Header.Get("Location") != "" || hits.Load() != 1 {
		t.Fatalf("cross-origin redirect followed: hits=%d error=%t", hits.Load(), err != nil)
	}
}
func TestOmissionMetadataLock(t *testing.T) {
	auth := &coreauth.Auth{Metadata: map[string]any{"setup_token": true}}
	var wg sync.WaitGroup
	start := make(chan struct{})
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 4000; i++ {
			claudeauth.StoreMetadataValue(&auth.Metadata, "other", i)
		}
	}()
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 4000; i++ {
			if !isClaudeSetupToken(auth, "sk-ant-oat-omission-probe") {
				t.Error("setup flag lost")
				return
			}
		}
	}()
	close(start)
	wg.Wait()
}
