package api

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestServerRoutes(t *testing.T) {
	srv := NewServer(slog.New(slog.NewJSONHandler(io.Discard, nil)))
	tests := []struct {
		name       string
		method     string
		path       string
		wantStatus int
		wantKey    string // dotted path into the JSON body; empty skips the body check
		wantValue  string
	}{
		{"healthz ok", http.MethodGet, "/healthz", http.StatusOK, "status", "ok"},
		{"unknown route", http.MethodGet, "/nope", http.StatusNotFound, "error.code", "not_found"},
		{"wrong method", http.MethodPost, "/healthz", http.StatusMethodNotAllowed, "", ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			srv.ServeHTTP(rec, httptest.NewRequest(tc.method, tc.path, nil))
			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d", rec.Code, tc.wantStatus)
			}
			if tc.wantKey == "" {
				return
			}
			var body map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("body is not JSON: %v (%q)", err, rec.Body.String())
			}
			if got := lookup(body, tc.wantKey); got != tc.wantValue {
				t.Fatalf("%s = %q, want %q", tc.wantKey, got, tc.wantValue)
			}
		})
	}
}

func lookup(body map[string]any, key string) string {
	if key == "error.code" {
		errObj, _ := body["error"].(map[string]any)
		code, _ := errObj["code"].(string)
		return code
	}
	v, _ := body[key].(string)
	return v
}
