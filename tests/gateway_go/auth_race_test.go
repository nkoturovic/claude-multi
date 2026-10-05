package auth

// The race pair ReconcileRegistryModelStates vs Auth.Clone, UNIT label.
// A real Manager publishes an auth clone, releases its lock and clones it again
// for the scheduler, while reconciliation replaces that object's ModelStates
// under the lock. This shows the mechanism only; the service-level scenario
// decides production reachability. Observation only; no network.
import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/router-for-me/CLIProxyAPI/v7/internal/registry"
)

func TestGatewayPair1ReconcileCloneUnit(t *testing.T) {
	rounds, err := strconv.Atoi(os.Getenv("GWTEST_ROUNDS"))
	if err != nil || rounds < 1 || rounds > 100000 {
		t.Fatal("GWTEST_ROUNDS must be an explicit positive bound")
	}
	ctx := context.Background()
	manager := NewManager(nil, nil, nil)
	id := "gwtest-pair1-unit"
	model := "gwtest-unit-model"
	seed := &Auth{ID: id, Provider: "claude", Status: StatusActive,
		Attributes: map[string]string{"api_key": "gwtest-unit-synthetic"},
		ModelStates: map[string]*ModelState{model: {Status: StatusError, Unavailable: true,
			NextRetryAfter: time.Now().Add(-time.Minute)}}}
	if _, err := manager.Register(ctx, seed); err != nil {
		t.Fatal(err)
	}
	registry.GetGlobalRegistry().RegisterClient(id, "claude", []*registry.ModelInfo{{ID: model}})
	defer registry.GetGlobalRegistry().UnregisterClient(id)
	var updates, reconciles int
	start := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < rounds; i++ {
			next := seed.Clone()
			next.Label = fmt.Sprintf("gwtest-%d", i)
			if _, err := manager.Update(ctx, next); err != nil {
				t.Error(err)
				return
			}
			updates++
		}
	}()
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < rounds; i++ {
			manager.ReconcileRegistryModelStates(ctx, id)
			reconciles++
		}
	}()
	close(start)
	wg.Wait()
	result, _ := json.Marshal(map[string]any{"scenario": "pair1-unit", "rounds": rounds,
		"updates": updates, "reconciles": reconciles})
	fmt.Println("GWTEST_RACE_RESULT=" + string(result))
}
