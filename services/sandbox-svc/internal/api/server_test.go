package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"repopilot/sandbox-svc/internal/sandbox"
)

const testToken = "test-token-0123456789"
const testID = "0123456789abcdef"

// fakeSandboxes returns err from every call when set, and otherwise canned results.
type fakeSandboxes struct {
	err       error
	execDelay time.Duration
	written   []byte
}

func (f *fakeSandboxes) Create(context.Context, string) (string, error) { return testID, f.err }
func (f *fakeSandboxes) Upload(_ context.Context, _ string, r io.Reader) error {
	_, _ = io.Copy(io.Discard, r)
	return f.err
}
func (f *fakeSandboxes) ReadFile(context.Context, string, string) ([]byte, error) {
	return []byte("content"), f.err
}
func (f *fakeSandboxes) WriteFile(_ context.Context, _, _ string, data []byte) error {
	f.written = data
	return f.err
}
func (f *fakeSandboxes) Exec(context.Context, string, sandbox.ExecRequest) (sandbox.ExecResponse, error) {
	time.Sleep(f.execDelay)
	return sandbox.ExecResponse{ExitCode: 0, Stdout: "ok"}, f.err
}
func (f *fakeSandboxes) Destroy(context.Context, string) error { return f.err }

func newTestServer(sb Sandboxes) http.Handler {
	return NewServer(slog.New(slog.NewJSONHandler(io.Discard, nil)), sb, testToken)
}

func do(t *testing.T, h http.Handler, method, path, token string, body io.Reader) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, body)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func errorCode(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Error struct{ Code string } `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v (%q)", err, rec.Body.String())
	}
	return body.Error.Code
}

func TestServerRoutes(t *testing.T) {
	h := newTestServer(&fakeSandboxes{})
	tests := []struct {
		name, method, path string
		wantStatus         int
		wantCode           string
	}{
		{"unknown route", http.MethodGet, "/nope", http.StatusNotFound, "not_found"},
		{"unclean path", http.MethodGet, "//nope", http.StatusNotFound, "not_found"},
		{"wrong method", http.MethodPost, "/healthz", http.StatusMethodNotAllowed, "method_not_allowed"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := do(t, h, tc.method, tc.path, "", nil)
			if rec.Code != tc.wantStatus || errorCode(t, rec) != tc.wantCode {
				t.Fatalf("got %d %q, want %d %q", rec.Code, rec.Body.String(), tc.wantStatus, tc.wantCode)
			}
		})
	}
	rec := do(t, h, http.MethodGet, "/healthz", "", nil)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"ok"`) {
		t.Fatalf("healthz = %d %q", rec.Code, rec.Body.String())
	}
}

var v1Routes = []struct{ method, path, body string }{
	{http.MethodPost, "/v1/sandboxes", `{"image":"repopilot-sandbox-base"}`},
	{http.MethodPut, "/v1/sandboxes/" + testID + "/archive", ""},
	{http.MethodGet, "/v1/sandboxes/" + testID + "/file?path=a.py", ""},
	{http.MethodPut, "/v1/sandboxes/" + testID + "/file?path=a.py", "x"},
	{http.MethodPost, "/v1/sandboxes/" + testID + "/exec", `{"argv":["ls"]}`},
	{http.MethodDelete, "/v1/sandboxes/" + testID, ""},
}

func TestAuthRequired(t *testing.T) {
	h := newTestServer(&fakeSandboxes{})
	for _, r := range v1Routes {
		for _, token := range []string{"", "wrong-token-0123456789"} {
			rec := do(t, h, r.method, r.path, token, strings.NewReader(r.body))
			if rec.Code != http.StatusUnauthorized || errorCode(t, rec) != "unauthorized" {
				t.Errorf("%s %s token=%q: %d %q", r.method, r.path, token, rec.Code, rec.Body.String())
			}
		}
	}
}

func TestRoutesSucceed(t *testing.T) {
	want := []int{http.StatusCreated, http.StatusNoContent, http.StatusOK, http.StatusNoContent,
		http.StatusOK, http.StatusNoContent}
	fake := &fakeSandboxes{}
	h := newTestServer(fake)
	for i, r := range v1Routes {
		rec := do(t, h, r.method, r.path, testToken, strings.NewReader(r.body))
		if rec.Code != want[i] {
			t.Errorf("%s %s: %d %q, want %d", r.method, r.path, rec.Code, rec.Body.String(), want[i])
		}
	}
	if string(fake.written) != "x" {
		t.Errorf("written = %q", fake.written)
	}
	rec := do(t, h, http.MethodGet, "/v1/sandboxes/"+testID+"/file?path=a.py", testToken, nil)
	if rec.Body.String() != "content" || rec.Header().Get("Content-Type") != "application/octet-stream" {
		t.Errorf("file body %q type %q", rec.Body.String(), rec.Header().Get("Content-Type"))
	}
	rec = do(t, h, http.MethodPost, "/v1/sandboxes", testToken, strings.NewReader(`{"image":"repopilot-x"}`))
	if !strings.Contains(rec.Body.String(), `"id":"`+testID+`"`) {
		t.Errorf("create body %q", rec.Body.String())
	}
}

func TestErrorMapping(t *testing.T) {
	tests := []struct {
		err    error
		status int
		code   string
	}{
		{sandbox.ErrImageNotAllowed, 400, "image_not_allowed"},
		{sandbox.ErrCommandNotAllowed, 400, "command_not_allowed"},
		{sandbox.ErrBadPath, 400, "bad_path"},
		{sandbox.ErrNotAFile, 400, "not_a_file"},
		{sandbox.ErrBadArgv, 400, "bad_argv"},
		{sandbox.ErrNotFound, 404, "not_found"},
		{sandbox.ErrTooLarge, 413, "too_large"},
		{fmt.Errorf("docker exploded: secret detail"), 500, "internal"},
	}
	for _, tc := range tests {
		h := newTestServer(&fakeSandboxes{err: fmt.Errorf("wrapped: %w", tc.err)})
		rec := do(t, h, http.MethodPost, "/v1/sandboxes/"+testID+"/exec", testToken, strings.NewReader(`{"argv":["ls"]}`))
		if rec.Code != tc.status || errorCode(t, rec) != tc.code {
			t.Errorf("%v: got %d %q", tc.err, rec.Code, rec.Body.String())
		}
		if tc.status == 500 && strings.Contains(rec.Body.String(), "secret detail") {
			t.Errorf("internal error message leaked: %q", rec.Body.String())
		}
	}
}

func TestFileTooLarge(t *testing.T) {
	h := newTestServer(&fakeSandboxes{})
	body := bytes.Repeat([]byte("x"), sandbox.MaxFileBytes+1)
	rec := do(t, h, http.MethodPut, "/v1/sandboxes/"+testID+"/file?path=a.py", testToken, bytes.NewReader(body))
	if rec.Code != http.StatusRequestEntityTooLarge || errorCode(t, rec) != "too_large" {
		t.Fatalf("got %d %q", rec.Code, rec.Body.String())
	}
}

func TestBadRequests(t *testing.T) {
	h := newTestServer(&fakeSandboxes{})
	for _, r := range []struct{ method, path, body, code string }{
		{http.MethodPost, "/v1/sandboxes/XYZ/exec", `{"argv":["ls"]}`, "not_found"},
		{http.MethodPost, "/v1/sandboxes/" + testID + "/exec", `not json`, "bad_request"},
		{http.MethodPost, "/v1/sandboxes", `{}`, "bad_request"},
		{http.MethodGet, "/v1/sandboxes/" + testID + "/file", "", "bad_path"},
	} {
		rec := do(t, h, r.method, r.path, testToken, strings.NewReader(r.body))
		if errorCode(t, rec) != r.code {
			t.Errorf("%s %s: %d %q, want %s", r.method, r.path, rec.Code, rec.Body.String(), r.code)
		}
	}
}

func TestExecExtendsWriteDeadline(t *testing.T) {
	srv := httptest.NewUnstartedServer(newTestServer(&fakeSandboxes{execDelay: 400 * time.Millisecond}))
	srv.Config.WriteTimeout = 200 * time.Millisecond
	srv.Start()
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/sandboxes/"+testID+"/exec",
		strings.NewReader(`{"argv":["ls"],"timeout_s":1}`))
	req.Header.Set("Authorization", "Bearer "+testToken)
	resp, err := srv.Client().Do(req)
	if err != nil {
		t.Fatalf("exec request failed (write deadline not extended?): %v", err)
	}
	defer resp.Body.Close()
	var out sandbox.ExecResponse
	if resp.StatusCode != http.StatusOK || json.NewDecoder(resp.Body).Decode(&out) != nil || out.Stdout != "ok" {
		t.Fatalf("status %d out %+v", resp.StatusCode, out)
	}
}
