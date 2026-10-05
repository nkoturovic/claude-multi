package api

// The race pairs UpdateClientsContext vs handlers and SetPluginHost vs
// interceptorHost, UNIT label. The real gin engine serves authenticated model
// lists and health while UpdateClientsContext applies genuinely changed
// configurations. No listener and no network: requests go to the engine
// in-process. The service-level scenario decides production reachability.
import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"

	proxyconfig "github.com/router-for-me/CLIProxyAPI/v7/internal/config"
	sdkconfig "github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
)

func TestGatewayReloadHandlersUnit(t *testing.T) {
	rounds, err := strconv.Atoi(os.Getenv("GWTEST_ROUNDS"))
	if err != nil || rounds < 1 || rounds > 100000 {
		t.Fatal("GWTEST_ROUNDS must be an explicit positive bound")
	}
	cfg := &proxyconfig.Config{SDKConfig: sdkconfig.SDKConfig{APIKeys: []string{"gwtest-unit-client"}}}
	server := newTestServerWithConfig(t, cfg)
	var statuses sync.Map
	var requests atomic.Int64
	stop := make(chan struct{})
	var wg sync.WaitGroup
	for worker := 0; worker < 4; worker++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				route := "/v1/models"
				if worker == 0 {
					route = "/healthz"
				}
				request := httptest.NewRequest(http.MethodGet, route, nil)
				request.Header.Set("Authorization", "Bearer gwtest-unit-client")
				recorder := httptest.NewRecorder()
				server.engine.ServeHTTP(recorder, request)
				key := fmt.Sprintf("%s:%d", route, recorder.Code)
				counter, _ := statuses.LoadOrStore(key, new(atomic.Int64))
				counter.(*atomic.Int64).Add(1)
				requests.Add(1)
			}
		}(worker)
	}
	applied := 0
	for i := 0; i < rounds; i++ {
		next := *server.cfg
		next.RequestRetry = i%3 + 1 // a genuinely changed configuration each round
		if server.UpdateClientsContext(context.Background(), &next) {
			applied++
		}
	}
	close(stop)
	wg.Wait()
	outcomes := map[string]int64{}
	statuses.Range(func(key, value any) bool {
		outcomes[key.(string)] = value.(*atomic.Int64).Load()
		return true
	})
	result, _ := json.Marshal(map[string]any{"scenario": "reload-handlers-unit", "rounds": rounds,
		"applied": applied, "requests": requests.Load(), "outcomes": outcomes})
	fmt.Println("GWTEST_RACE_RESULT=" + string(result))
}
