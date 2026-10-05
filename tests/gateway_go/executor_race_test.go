package executor

// Shared Claude credential metadata, direct-executor label ONLY. One shared
// Auth is handed to many goroutines through the exported entry points,
// bypassing the manager's per-credential lock and metadata clones. A detector
// report here is a unit reproduction, never evidence that ordinary HTTP
// traffic races.
// Observation only; built by gateway-race.nix, run by check_gateway_races.py
// inside bwrap --unshare-net. No network: the profile fetcher is a counter.
import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"

	claudeauth "github.com/router-for-me/CLIProxyAPI/v7/internal/auth/claude"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v7/sdk/cliproxy/auth"
)

func gwtestRounds(t *testing.T) int {
	rounds, err := strconv.Atoi(os.Getenv("GWTEST_ROUNDS"))
	if err != nil || rounds < 1 || rounds > 10000 {
		t.Fatal("GWTEST_ROUNDS must be an explicit positive bound")
	}
	return rounds
}

// A setup-token credential (the flag lives in shared metadata, as it does for
// file auths) never reaches the profile fetcher: isClaudeSetupToken reads the
// flag map while concurrent preparers store account_uuid into the same map.
func TestGatewayB108DirectSetupToken(t *testing.T) {
	rounds := gwtestRounds(t)
	executor := NewClaudeExecutor(&config.Config{})
	var fetches atomic.Int64
	executor.oauthProfileFetcher = func(context.Context, *cliproxyauth.Auth, string) (*claudeauth.OAuthProfile, error) {
		fetches.Add(1)
		return nil, fmt.Errorf("gwtest: profile fetch must not run for a setup token")
	}
	var prepared, errors atomic.Int64
	for round := 0; round < rounds; round++ {
		auth := &cliproxyauth.Auth{
			ID:         fmt.Sprintf("gwtest-b108-direct-%d", round),
			Provider:   "claude",
			Attributes: map[string]string{"api_key": "sk-ant-oat-gwtest-direct-synthetic"},
			Metadata:   map[string]any{"is_setup_token": true},
		}
		start := make(chan struct{})
		var wg sync.WaitGroup
		for worker := 0; worker < 32; worker++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				if !executor.ShouldPrepareRequestAuth(auth) {
					return
				}
				if _, err := executor.PrepareRequestAuth(context.Background(), auth); err != nil {
					errors.Add(1)
					return
				}
				prepared.Add(1)
			}()
		}
		close(start)
		wg.Wait()
	}
	result, _ := json.Marshal(map[string]any{
		"scenario": "b108-direct-setup-token", "rounds": rounds, "goroutines": 32,
		"prepared": prepared.Load(), "errors": errors.Load(), "profile_fetches": fetches.Load(),
	})
	fmt.Println("GWTEST_RACE_RESULT=" + string(result))
	if fetches.Load() != 0 || errors.Load() != 0 {
		t.Fatal("setup-token preparation left the offline path")
	}
}
