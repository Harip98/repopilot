// Package api is sandbox-svc's HTTP surface: the only way the agent reaches a sandbox.
package api

import (
	"encoding/json"
	"log/slog"
	"net/http"
)

// NewServer returns the sandbox-svc router.
func NewServer(logger *slog.Logger) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h, pattern := mux.Handler(r)
		if pattern != "" {
			h.ServeHTTP(w, r)
			return
		}
		// No route matched: let the mux decide 404 vs 405 (and set Allow), then answer
		// in the same {"error":{code,message}} shape as agent-svc.
		rec := &statusRecorder{header: http.Header{}}
		h.ServeHTTP(rec, r)
		if allow := rec.header.Get("Allow"); allow != "" {
			w.Header().Set("Allow", allow)
		}
		code := "not_found"
		if rec.code == http.StatusMethodNotAllowed {
			code = "method_not_allowed"
		}
		logger.Info("unrouted request", "method", r.Method, "path", r.URL.Path, "status", rec.code)
		writeError(w, rec.code, code, http.StatusText(rec.code))
	})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]map[string]string{"error": {"code": code, "message": message}})
}

// statusRecorder captures the status and headers of the mux's fallback handler and drops its
// plain-text body.
type statusRecorder struct {
	header http.Header
	code   int
}

func (s *statusRecorder) Header() http.Header         { return s.header }
func (s *statusRecorder) Write(b []byte) (int, error) { return len(b), nil }
func (s *statusRecorder) WriteHeader(code int)        { s.code = code }
