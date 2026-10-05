package handlers

// Seams that predate the admitted series stay compilable in every
// dependency-closed omission.
import (
	"context"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/config"
)

func TestOmissionConfigSnapshot(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := NewBaseAPIHandlers(&config.SDKConfig{}, nil)
	start := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 4000; i++ {
			h.UpdateClients(&config.SDKConfig{})
		}
	}()
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 4000; i++ {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest("POST", "/v1/messages", nil)
			stop := h.StartNonStreamingKeepAlive(c, context.Background())
			stop()
		}
	}()
	close(start)
	wg.Wait()
}

func TestOmissionPluginHostLock(t *testing.T) {
	h := NewBaseAPIHandlers(&config.SDKConfig{}, nil)
	start := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 10000; i++ {
			h.SetPluginHost(nil)
		}
	}()
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 10000; i++ {
			if h.interceptorHost() != nil {
				t.Error("unexpected host")
				return
			}
		}
	}()
	close(start)
	wg.Wait()
}
