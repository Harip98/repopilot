package docker

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCreateContainerSendsHostConfig(t *testing.T) {
	var body map[string]any
	var gotName string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/containers/create" {
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
		}
		gotName = r.URL.Query().Get("name")
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"Id":"abc123"}`)
	}))
	defer srv.Close()

	c := NewHTTPClient(srv.URL, srv.Client())
	id, err := c.CreateContainer(context.Background(), "rp-sbx-1", ContainerSpec{
		Image: "repopilot-sandbox-base", User: "10001:10001", WorkingDir: "/workspace",
		Env: []string{"HOME=/tmp"}, Cmd: []string{"sleep", "infinity"},
		Labels: map[string]string{"repopilot.sandbox": "1"},
		Host: HostConfig{
			NetworkMode: "none", CapDrop: []string{"ALL"},
			SecurityOpt: []string{"no-new-privileges:true"}, ReadonlyRootfs: true,
			Init: true, IpcMode: "private", Memory: 1 << 30, MemorySwap: 1 << 30,
			NanoCpus: 1e9, PidsLimit: 256,
			Tmpfs:  map[string]string{"/tmp": "rw,nosuid,nodev,size=256m"},
			Mounts: []Mount{{Type: "volume", Source: "rp-ws-1", Target: "/workspace"}},
		},
	})
	if err != nil || id != "abc123" || gotName != "rp-sbx-1" {
		t.Fatalf("id=%q name=%q err=%v", id, gotName, err)
	}
	host := body["HostConfig"].(map[string]any)
	want := map[string]any{
		"NetworkMode": "none", "ReadonlyRootfs": true, "Init": true, "IpcMode": "private",
		"Privileged": false, "Memory": float64(1 << 30), "MemorySwap": float64(1 << 30),
		"NanoCpus": float64(1e9), "PidsLimit": float64(256),
	}
	for k, v := range want {
		if host[k] != v {
			t.Errorf("HostConfig.%s = %v, want %v", k, host[k], v)
		}
	}
	if caps := host["CapDrop"].([]any); len(caps) != 1 || caps[0] != "ALL" {
		t.Errorf("CapDrop = %v", caps)
	}
	if opts := host["SecurityOpt"].([]any); len(opts) != 1 || opts[0] != "no-new-privileges:true" {
		t.Errorf("SecurityOpt = %v", opts)
	}
	if mounts := host["Mounts"].([]any); len(mounts) != 1 {
		t.Errorf("Mounts = %v", mounts)
	}
	if body["User"] != "10001:10001" || body["Image"] != "repopilot-sandbox-base" {
		t.Errorf("User/Image = %v/%v", body["User"], body["Image"])
	}
}

func TestExecFlow(t *testing.T) {
	var execCmd []any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/containers/c1/exec":
			var req map[string]any
			_ = json.NewDecoder(r.Body).Decode(&req)
			execCmd = req["Cmd"].([]any)
			w.WriteHeader(http.StatusCreated)
			_, _ = io.WriteString(w, `{"Id":"e1"}`)
		case r.Method == http.MethodPost && r.URL.Path == "/exec/e1/start":
			w.Header().Set("Content-Type", "application/vnd.docker.multiplexed-stream")
			_, _ = w.Write(frame(1, []byte("hello")))
			_, _ = w.Write(frame(2, []byte("oops")))
		case r.Method == http.MethodGet && r.URL.Path == "/exec/e1/json":
			_, _ = io.WriteString(w, `{"ExitCode":3,"Running":false}`)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	c := NewHTTPClient(srv.URL, srv.Client())
	res, err := c.Exec(context.Background(), "c1", []string{"python", "-V"}, "/workspace", 1024)
	if err != nil {
		t.Fatal(err)
	}
	if res.ExitCode != 3 || res.Stdout != "hello" || res.Stderr != "oops" {
		t.Fatalf("res = %+v", res)
	}
	if len(execCmd) != 2 || execCmd[0] != "python" {
		t.Fatalf("exec Cmd = %v", execCmd)
	}
}

func TestGetArchiveNotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"message":"Could not find the file /workspace/x"}`)
	}))
	defer srv.Close()

	_, err := NewHTTPClient(srv.URL, srv.Client()).GetArchive(context.Background(), "c1", "/workspace/x")
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("err = %v, want ErrNotFound", err)
	}
}

func TestAPIErrorCarriesMessage(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = io.WriteString(w, `{"message":"volume in use"}`)
	}))
	defer srv.Close()

	err := NewHTTPClient(srv.URL, srv.Client()).RemoveVolume(context.Background(), "v")
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Status != 409 || !strings.Contains(apiErr.Message, "in use") {
		t.Fatalf("err = %v", err)
	}
}
