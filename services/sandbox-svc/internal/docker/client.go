// Package docker is a minimal Docker Engine API client: only the calls sandbox-svc needs,
// over the daemon's unix socket, with no third-party dependencies.
package docker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
)

// ErrNotFound matches any 404 from the daemon (missing container, volume, exec or file).
var ErrNotFound = errors.New("not found")

// APIError is a non-2xx daemon response.
type APIError struct {
	Status  int
	Message string
}

func (e *APIError) Error() string { return fmt.Sprintf("docker API %d: %s", e.Status, e.Message) }

func (e *APIError) Unwrap() error {
	if e.Status == http.StatusNotFound {
		return ErrNotFound
	}
	return nil
}

// Mount is one entry of HostConfig.Mounts.
type Mount struct {
	Type     string `json:"Type"`
	Source   string `json:"Source"`
	Target   string `json:"Target"`
	ReadOnly bool   `json:"ReadOnly"`
}

// HostConfig carries the isolation settings; field names are the Engine API's.
type HostConfig struct {
	NetworkMode    string            `json:"NetworkMode"`
	CapDrop        []string          `json:"CapDrop"`
	SecurityOpt    []string          `json:"SecurityOpt"`
	ReadonlyRootfs bool              `json:"ReadonlyRootfs"`
	Init           bool              `json:"Init"`
	IpcMode        string            `json:"IpcMode"`
	Privileged     bool              `json:"Privileged"`
	Memory         int64             `json:"Memory"`
	MemorySwap     int64             `json:"MemorySwap"`
	NanoCpus       int64             `json:"NanoCpus"`
	PidsLimit      int64             `json:"PidsLimit"`
	Tmpfs          map[string]string `json:"Tmpfs"`
	Mounts         []Mount           `json:"Mounts"`
}

// ContainerSpec is the body of POST /containers/create.
type ContainerSpec struct {
	Image      string            `json:"Image"`
	User       string            `json:"User"`
	Env        []string          `json:"Env"`
	WorkingDir string            `json:"WorkingDir"`
	Cmd        []string          `json:"Cmd"`
	Labels     map[string]string `json:"Labels"`
	Host       HostConfig        `json:"HostConfig"`
}

// ContainerSummary is one row of GET /containers/json.
type ContainerSummary struct {
	ID     string            `json:"Id"`
	Names  []string          `json:"Names"`
	Labels map[string]string `json:"Labels"`
}

// ExecResult is a finished exec: exit code plus capped, UTF-8-clean output.
type ExecResult struct {
	ExitCode        int
	Stdout          string
	Stderr          string
	StdoutTruncated bool
	StderrTruncated bool
}

// Client talks to one Docker daemon. Requests carry no API version prefix, so the daemon
// answers with its own current version.
type Client struct {
	base string
	hc   *http.Client
}

// NewUnixClient dials the daemon's unix socket. There is no client-wide timeout: execs can
// legitimately run for minutes, so every call is bounded by its context instead.
func NewUnixClient(sock string) *Client {
	tr := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "unix", sock)
		},
	}
	return &Client{base: "http://docker", hc: &http.Client{Transport: tr}}
}

// NewHTTPClient points at an HTTP endpoint; used by tests with a fake daemon.
func NewHTTPClient(baseURL string, hc *http.Client) *Client {
	return &Client{base: baseURL, hc: hc}
}

func (c *Client) do(ctx context.Context, method, path string, query url.Values, body io.Reader,
	contentType string) (*http.Response, error) {
	u := c.base + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, body)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 {
		defer resp.Body.Close()
		var msg struct {
			Message string `json:"message"`
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
		if json.Unmarshal(raw, &msg) != nil || msg.Message == "" {
			msg.Message = string(raw)
		}
		return nil, &APIError{Status: resp.StatusCode, Message: msg.Message}
	}
	return resp, nil
}

func (c *Client) doJSON(ctx context.Context, method, path string, query url.Values, in, out any) error {
	var body io.Reader
	contentType := ""
	if in != nil {
		raw, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body, contentType = bytes.NewReader(raw), "application/json"
	}
	resp, err := c.do(ctx, method, path, query, body, contentType)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if out == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) CreateVolume(ctx context.Context, name string, driverOpts, labels map[string]string) error {
	in := map[string]any{"Name": name, "Driver": "local", "DriverOpts": driverOpts, "Labels": labels}
	return c.doJSON(ctx, http.MethodPost, "/volumes/create", nil, in, nil)
}

func (c *Client) RemoveVolume(ctx context.Context, name string) error {
	return c.doJSON(ctx, http.MethodDelete, "/volumes/"+url.PathEscape(name), nil, nil, nil)
}

func (c *Client) CreateContainer(ctx context.Context, name string, spec ContainerSpec) (string, error) {
	var out struct {
		ID string `json:"Id"`
	}
	err := c.doJSON(ctx, http.MethodPost, "/containers/create", url.Values{"name": {name}}, spec, &out)
	return out.ID, err
}

func (c *Client) StartContainer(ctx context.Context, id string) error {
	return c.doJSON(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/start", nil, nil, nil)
}

func (c *Client) RestartContainer(ctx context.Context, id string) error {
	return c.doJSON(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/restart",
		url.Values{"t": {"0"}}, nil, nil)
}

func (c *Client) RemoveContainer(ctx context.Context, id string) error {
	return c.doJSON(ctx, http.MethodDelete, "/containers/"+url.PathEscape(id),
		url.Values{"force": {"true"}, "v": {"true"}}, nil, nil)
}

func (c *Client) ListContainers(ctx context.Context, label string) ([]ContainerSummary, error) {
	filters, err := json.Marshal(map[string][]string{"label": {label}})
	if err != nil {
		return nil, err
	}
	var out []ContainerSummary
	err = c.doJSON(ctx, http.MethodGet, "/containers/json",
		url.Values{"all": {"true"}, "filters": {string(filters)}}, nil, &out)
	return out, err
}

// PutArchive extracts a tar stream into dir inside the container. copyUIDGID makes the daemon
// chown the extracted files to the container's user, so the non-root sandbox can write them.
func (c *Client) PutArchive(ctx context.Context, id, dir string, tar io.Reader) error {
	resp, err := c.do(ctx, http.MethodPut, "/containers/"+url.PathEscape(id)+"/archive",
		url.Values{"path": {dir}, "copyUIDGID": {"true"}}, tar, "application/x-tar")
	if err != nil {
		return err
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.Body.Close()
}

// GetArchive returns a tar stream of path inside the container. The caller closes it.
func (c *Client) GetArchive(ctx context.Context, id, path string) (io.ReadCloser, error) {
	resp, err := c.do(ctx, http.MethodGet, "/containers/"+url.PathEscape(id)+"/archive",
		url.Values{"path": {path}}, nil, "")
	if err != nil {
		return nil, err
	}
	return resp.Body, nil
}

// Exec runs argv in the container and waits for it to finish, keeping at most maxOut bytes
// of each output stream.
func (c *Client) Exec(ctx context.Context, id string, argv []string, workdir string, maxOut int) (ExecResult, error) {
	var created struct {
		ID string `json:"Id"`
	}
	in := map[string]any{
		"AttachStdout": true, "AttachStderr": true, "Tty": false,
		"Cmd": argv, "WorkingDir": workdir,
	}
	if err := c.doJSON(ctx, http.MethodPost, "/containers/"+url.PathEscape(id)+"/exec", nil, in, &created); err != nil {
		return ExecResult{}, fmt.Errorf("exec create: %w", err)
	}
	start, _ := json.Marshal(map[string]bool{"Detach": false, "Tty": false})
	resp, err := c.do(ctx, http.MethodPost, "/exec/"+url.PathEscape(created.ID)+"/start", nil,
		bytes.NewReader(start), "application/json")
	if err != nil {
		return ExecResult{}, fmt.Errorf("exec start: %w", err)
	}
	stdout, stderr := &CappedBuffer{Max: maxOut}, &CappedBuffer{Max: maxOut}
	demuxErr := Demux(resp.Body, stdout, stderr)
	resp.Body.Close()
	if demuxErr != nil {
		return ExecResult{}, fmt.Errorf("exec stream: %w", demuxErr)
	}
	var inspect struct {
		ExitCode int  `json:"ExitCode"`
		Running  bool `json:"Running"`
	}
	if err := c.doJSON(ctx, http.MethodGet, "/exec/"+url.PathEscape(created.ID)+"/json", nil, nil, &inspect); err != nil {
		return ExecResult{}, fmt.Errorf("exec inspect: %w", err)
	}
	return ExecResult{
		ExitCode: inspect.ExitCode, Stdout: stdout.String(), Stderr: stderr.String(),
		StdoutTruncated: stdout.Truncated(), StderrTruncated: stderr.Truncated(),
	}, nil
}
