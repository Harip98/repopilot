// Package api is sandbox-svc's HTTP surface: the only way the agent reaches a sandbox.
package api

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"time"

	"repopilot/sandbox-svc/internal/sandbox"
)

// Sandboxes is what the API needs from the sandbox manager.
type Sandboxes interface {
	Create(ctx context.Context, image string) (string, error)
	Upload(ctx context.Context, id string, r io.Reader) error
	ReadFile(ctx context.Context, id, path string) ([]byte, error)
	WriteFile(ctx context.Context, id, path string, data []byte) error
	Exec(ctx context.Context, id string, req sandbox.ExecRequest) (sandbox.ExecResponse, error)
	Destroy(ctx context.Context, id string) error
}

var sandboxID = regexp.MustCompile(`^[0-9a-f]{16}$`)

const maxJSONBody = 64 << 10

// NewServer returns the sandbox-svc router. Every /v1 route needs the bearer token: this
// service holds the Docker socket, so even a loopback caller must authenticate.
func NewServer(logger *slog.Logger, sb Sandboxes, token string) http.Handler {
	h := &handlers{log: logger, sb: sb}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	auth := requireToken(token)
	mux.Handle("POST /v1/sandboxes", auth(http.HandlerFunc(h.create)))
	mux.Handle("PUT /v1/sandboxes/{id}/archive", auth(withID(h.upload)))
	mux.Handle("GET /v1/sandboxes/{id}/file", auth(withID(h.readFile)))
	mux.Handle("PUT /v1/sandboxes/{id}/file", auth(withID(h.writeFile)))
	mux.Handle("POST /v1/sandboxes/{id}/exec", auth(withID(h.exec)))
	mux.Handle("DELETE /v1/sandboxes/{id}", auth(withID(h.destroy)))

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hd, pattern := mux.Handler(r)
		if pattern != "" {
			mux.ServeHTTP(w, r) // not hd.ServeHTTP: only the mux fills in r.PathValue
			return
		}
		// No route matched. Keep the mux's 405 (and its Allow header); everything else,
		// including the mux's redirects for unclean paths, is a JSON 404.
		rec := &statusRecorder{header: http.Header{}}
		hd.ServeHTTP(rec, r)
		logger.Info("unrouted request", "method", r.Method, "path", r.URL.Path, "status", rec.code)
		if rec.code == http.StatusMethodNotAllowed {
			w.Header().Set("Allow", rec.header.Get("Allow"))
			writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "Method Not Allowed")
			return
		}
		writeError(w, http.StatusNotFound, "not_found", "Not Found")
	})
}

func requireToken(token string) func(http.Handler) http.Handler {
	want := []byte("Bearer " + token)
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			got := []byte(r.Header.Get("Authorization"))
			if subtle.ConstantTimeCompare(got, want) != 1 {
				writeError(w, http.StatusUnauthorized, "unauthorized", "missing or invalid bearer token")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

func withID(next func(http.ResponseWriter, *http.Request, string)) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		if !sandboxID.MatchString(id) {
			writeError(w, http.StatusNotFound, "not_found", "no such sandbox")
			return
		}
		next(w, r, id)
	})
}

type handlers struct {
	log *slog.Logger
	sb  Sandboxes
}

func (h *handlers) create(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Image string `json:"image"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxJSONBody)).Decode(&req); err != nil || req.Image == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "body must be {\"image\": \"...\"}")
		return
	}
	id, err := h.sb.Create(r.Context(), req.Image)
	if err != nil {
		h.fail(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]string{"id": id})
}

func (h *handlers) upload(w http.ResponseWriter, r *http.Request, id string) {
	if err := h.sb.Upload(r.Context(), id, r.Body); err != nil {
		h.fail(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// filePath returns the required ?path= parameter; the manager validates its shape.
func filePath(w http.ResponseWriter, r *http.Request) (string, bool) {
	p := r.URL.Query().Get("path")
	if p == "" {
		writeError(w, http.StatusBadRequest, "bad_path", "missing ?path=")
	}
	return p, p != ""
}

func (h *handlers) readFile(w http.ResponseWriter, r *http.Request, id string) {
	p, ok := filePath(w, r)
	if !ok {
		return
	}
	data, err := h.sb.ReadFile(r.Context(), id, p)
	if err != nil {
		h.fail(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}

func (h *handlers) writeFile(w http.ResponseWriter, r *http.Request, id string) {
	p, ok := filePath(w, r)
	if !ok {
		return
	}
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, sandbox.MaxFileBytes))
	var tooBig *http.MaxBytesError
	if errors.As(err, &tooBig) {
		writeError(w, http.StatusRequestEntityTooLarge, "too_large", "file exceeds 1 MiB")
		return
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "could not read body")
		return
	}
	if err := h.sb.WriteFile(r.Context(), id, p, data); err != nil {
		h.fail(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (h *handlers) exec(w http.ResponseWriter, r *http.Request, id string) {
	var req sandbox.ExecRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxJSONBody)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "body must be an exec request")
		return
	}
	// An exec may legitimately run far longer than the server-wide WriteTimeout, including the
	// cleanup or reset that follows it.
	limit := time.Duration(sandbox.ClampTimeout(req.TimeoutS))*time.Second + sandbox.ExecOverhead
	if err := http.NewResponseController(w).SetWriteDeadline(time.Now().Add(limit)); err != nil {
		h.log.Warn("could not extend write deadline", "err", err)
	}
	res, err := h.sb.Exec(r.Context(), id, req)
	if err != nil {
		h.fail(w, err)
		return
	}
	h.log.Info("exec", "id", id, "cmd", req.Argv[0], "exit", res.ExitCode,
		"timed_out", res.TimedOut, "ms", res.DurationMs)
	writeJSON(w, http.StatusOK, res)
}

func (h *handlers) destroy(w http.ResponseWriter, r *http.Request, id string) {
	if err := h.sb.Destroy(r.Context(), id); err != nil {
		h.fail(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

var errorCodes = []struct {
	err    error
	status int
	code   string
}{
	{sandbox.ErrImageNotAllowed, http.StatusBadRequest, "image_not_allowed"},
	{sandbox.ErrCommandNotAllowed, http.StatusBadRequest, "command_not_allowed"},
	{sandbox.ErrBadPath, http.StatusBadRequest, "bad_path"},
	{sandbox.ErrNotAFile, http.StatusBadRequest, "not_a_file"},
	{sandbox.ErrBadArgv, http.StatusBadRequest, "bad_argv"},
	{sandbox.ErrNotFound, http.StatusNotFound, "not_found"},
	{sandbox.ErrTooLarge, http.StatusRequestEntityTooLarge, "too_large"},
	{sandbox.ErrBadArchive, http.StatusBadRequest, "bad_archive"},
	{sandbox.ErrSandboxExhausted, http.StatusConflict, "sandbox_exhausted"},
}

// fail maps manager errors to HTTP. Unknown errors are logged and answered without detail.
func (h *handlers) fail(w http.ResponseWriter, err error) {
	for _, e := range errorCodes {
		if errors.Is(err, e.err) {
			writeError(w, e.status, e.code, err.Error())
			return
		}
	}
	h.log.Error("internal error", "err", err)
	writeError(w, http.StatusInternalServerError, "internal", "internal error")
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
