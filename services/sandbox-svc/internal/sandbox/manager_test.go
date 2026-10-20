package sandbox

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"repopilot/sandbox-svc/internal/docker"
)

// fakeEngine records calls and returns scripted results.
type fakeEngine struct {
	mu          sync.Mutex
	volumes     map[string]map[string]string // name -> driver opts
	containers  map[string]docker.ContainerSpec
	started     []string
	removedCtrs []string
	removedVols []string
	listed      []docker.ContainerSummary
	execs       [][]string
	execFn      func(ctx context.Context, argv []string) (docker.ExecResult, error)
	archive     []byte // returned by GetArchive
	archiveErr  error
	put         []byte // last PutArchive body
	putDir      string
}

func newFakeEngine() *fakeEngine {
	return &fakeEngine{volumes: map[string]map[string]string{}, containers: map[string]docker.ContainerSpec{}}
}

func (f *fakeEngine) CreateVolume(_ context.Context, name string, opts, _ map[string]string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.volumes[name] = opts
	return nil
}

func (f *fakeEngine) RemoveVolume(_ context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removedVols = append(f.removedVols, name)
	return nil
}

func (f *fakeEngine) CreateContainer(_ context.Context, name string, spec docker.ContainerSpec) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.containers[name] = spec
	return "cid-" + name, nil
}

func (f *fakeEngine) StartContainer(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.started = append(f.started, id)
	return nil
}

func (f *fakeEngine) RemoveContainer(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removedCtrs = append(f.removedCtrs, id)
	return nil
}

func (f *fakeEngine) ListContainers(context.Context, string) ([]docker.ContainerSummary, error) {
	return f.listed, nil
}

func (f *fakeEngine) PutArchive(_ context.Context, _, dir string, r io.Reader) error {
	b, err := io.ReadAll(r)
	f.put, f.putDir = b, dir
	return err
}

func (f *fakeEngine) GetArchive(context.Context, string, string) (io.ReadCloser, error) {
	if f.archiveErr != nil {
		return nil, f.archiveErr
	}
	return io.NopCloser(bytes.NewReader(f.archive)), nil
}

func (f *fakeEngine) Exec(ctx context.Context, _ string, argv []string, _ string, _ int) (docker.ExecResult, error) {
	f.mu.Lock()
	f.execs = append(f.execs, argv)
	fn := f.execFn
	f.mu.Unlock()
	if fn != nil {
		return fn(ctx, argv)
	}
	return docker.ExecResult{}, nil
}

func newTestManager(e Engine) *Manager {
	m := NewManager(e, Config{ImagePrefixes: []string{"repopilot-"}, TTL: 2 * time.Hour,
		MaxOutput: 64 * 1024, Now: time.Now}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	return m
}

func tarOf(t *testing.T, hdr *tar.Header, data []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := tw.WriteHeader(hdr); err != nil {
		t.Fatal(err)
	}
	if len(data) > 0 {
		if _, err := tw.Write(data); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestCreateRejectsImage(t *testing.T) {
	e := newFakeEngine()
	if _, err := newTestManager(e).Create(context.Background(), "python:3.12"); !errors.Is(err, ErrImageNotAllowed) {
		t.Fatalf("err = %v", err)
	}
	if len(e.volumes)+len(e.containers) != 0 {
		t.Fatal("engine was called for a rejected image")
	}
}

func TestCreateBuildsHardenedSandbox(t *testing.T) {
	e := newFakeEngine()
	id, err := newTestManager(e).Create(context.Background(), "repopilot-sandbox-base")
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[0-9a-f]{16}$`).MatchString(id) {
		t.Fatalf("id = %q", id)
	}
	opts, ok := e.volumes["rp-ws-"+id]
	if !ok || opts["type"] != "tmpfs" || opts["device"] != "tmpfs" ||
		opts["o"] != "size=512m,uid=10001,gid=10001" {
		t.Fatalf("volume opts = %v (found %v)", opts, ok)
	}
	spec, ok := e.containers["rp-sbx-"+id]
	if !ok {
		t.Fatal("container not created")
	}
	h := spec.Host
	if h.NetworkMode != "none" || !h.ReadonlyRootfs || !h.Init || h.Privileged ||
		h.IpcMode != "private" || h.Memory != 1<<30 || h.MemorySwap != 1<<30 ||
		h.NanoCpus != 1e9 || h.PidsLimit != 256 {
		t.Fatalf("host config = %+v", h)
	}
	if len(h.CapDrop) != 1 || h.CapDrop[0] != "ALL" ||
		len(h.SecurityOpt) != 1 || h.SecurityOpt[0] != "no-new-privileges:true" {
		t.Fatalf("caps/secopt = %v / %v", h.CapDrop, h.SecurityOpt)
	}
	if h.Tmpfs["/tmp"] != "rw,nosuid,nodev,size=256m" {
		t.Fatalf("tmpfs = %v", h.Tmpfs)
	}
	if len(h.Mounts) != 1 || h.Mounts[0].Source != "rp-ws-"+id || h.Mounts[0].Target != "/workspace" {
		t.Fatalf("mounts = %+v", h.Mounts)
	}
	if spec.User != "10001:10001" || spec.WorkingDir != "/workspace" ||
		spec.Labels["repopilot.sandbox"] != "1" || spec.Labels["repopilot.created"] == "" {
		t.Fatalf("spec = %+v", spec)
	}
	env := strings.Join(spec.Env, " ")
	if !strings.Contains(env, "HOME=/tmp") || !strings.Contains(env, "PYTHONDONTWRITEBYTECODE=1") {
		t.Fatalf("env = %v", spec.Env)
	}
	if len(e.started) != 1 || e.started[0] != "rp-sbx-"+id {
		t.Fatalf("started = %v", e.started)
	}
}

func TestExecWrapsWithTimeout(t *testing.T) {
	e := newFakeEngine()
	m := newTestManager(e)
	if _, err := m.Exec(context.Background(), "0123456789abcdef", ExecRequest{Argv: []string{"pytest", "-q"}}); err != nil {
		t.Fatal(err)
	}
	want := []string{"timeout", "-k", "5", "120s", "pytest", "-q"}
	if got := e.execs[0]; strings.Join(got, " ") != strings.Join(want, " ") {
		t.Fatalf("argv = %v, want %v", got, want)
	}
	if _, err := m.Exec(context.Background(), "0123456789abcdef", ExecRequest{Argv: []string{"sh"}}); !errors.Is(err, ErrCommandNotAllowed) {
		t.Fatalf("err = %v", err)
	}
	if _, err := m.Exec(context.Background(), "0123456789abcdef", ExecRequest{Argv: []string{"ls"}, Cwd: "../x"}); !errors.Is(err, ErrBadPath) {
		t.Fatalf("err = %v", err)
	}
}

func TestExecTimedOut(t *testing.T) {
	e := newFakeEngine()
	e.execFn = func(context.Context, []string) (docker.ExecResult, error) {
		return docker.ExecResult{ExitCode: 124}, nil
	}
	res, err := newTestManager(e).Exec(context.Background(), "0123456789abcdef", ExecRequest{Argv: []string{"python"}})
	if err != nil || !res.TimedOut || res.ExitCode != 124 {
		t.Fatalf("res = %+v err = %v", res, err)
	}
}

func TestExecBackstopKillsProcesses(t *testing.T) {
	e := newFakeEngine()
	e.execFn = func(ctx context.Context, argv []string) (docker.ExecResult, error) {
		if argv[0] == killAllCmd {
			return docker.ExecResult{}, nil
		}
		<-ctx.Done() // a child kept the output stream open past the deadline
		return docker.ExecResult{}, ctx.Err()
	}
	m := newTestManager(e)
	m.backstopGrace = 100 * time.Millisecond
	start := time.Now()
	res, err := m.Exec(context.Background(), "0123456789abcdef", ExecRequest{Argv: []string{"python"}, TimeoutS: 1})
	if err != nil {
		t.Fatal(err)
	}
	if !res.TimedOut || res.ExitCode != -1 {
		t.Fatalf("res = %+v", res)
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("backstop took %v", elapsed)
	}
	if last := e.execs[len(e.execs)-1]; len(last) != 1 || last[0] != killAllCmd {
		t.Fatalf("last exec = %v, want [%s]", last, killAllCmd)
	}
}

func TestReadFile(t *testing.T) {
	e := newFakeEngine()
	m := newTestManager(e)
	e.archive = tarOf(t, &tar.Header{Name: "a.py", Typeflag: tar.TypeReg, Size: 2, Mode: 0o644}, []byte("hi"))
	got, err := m.ReadFile(context.Background(), "0123456789abcdef", "a.py")
	if err != nil || string(got) != "hi" {
		t.Fatalf("got %q err %v", got, err)
	}
}

func TestReadFileRejectsSymlink(t *testing.T) {
	e := newFakeEngine()
	e.archive = tarOf(t, &tar.Header{Name: "leak", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"}, nil)
	if _, err := newTestManager(e).ReadFile(context.Background(), "0123456789abcdef", "leak"); !errors.Is(err, ErrNotAFile) {
		t.Fatalf("err = %v", err)
	}
}

func TestReadFileTooLarge(t *testing.T) {
	e := newFakeEngine()
	big := bytes.Repeat([]byte("x"), MaxFileBytes+1)
	e.archive = tarOf(t, &tar.Header{Name: "big", Typeflag: tar.TypeReg, Size: int64(len(big)), Mode: 0o644}, big)
	if _, err := newTestManager(e).ReadFile(context.Background(), "0123456789abcdef", "big"); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("err = %v", err)
	}
}

func TestReadFileMissing(t *testing.T) {
	e := newFakeEngine()
	e.archiveErr = &docker.APIError{Status: 404, Message: "no such file"}
	if _, err := newTestManager(e).ReadFile(context.Background(), "0123456789abcdef", "nope.py"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("err = %v", err)
	}
}

func TestWriteFileTar(t *testing.T) {
	e := newFakeEngine()
	if err := newTestManager(e).WriteFile(context.Background(), "0123456789abcdef", "./pkg/sub/new.py", []byte("x = 1\n")); err != nil {
		t.Fatal(err)
	}
	if e.putDir != "/workspace" {
		t.Fatalf("put dir = %q", e.putDir)
	}
	tr := tar.NewReader(bytes.NewReader(e.put))
	var names []string
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names = append(names, hdr.Name)
		if hdr.Uid != 10001 || hdr.Gid != 10001 {
			t.Fatalf("%s owned by %d:%d", hdr.Name, hdr.Uid, hdr.Gid)
		}
		if hdr.Typeflag == tar.TypeReg {
			data, _ := io.ReadAll(tr)
			if hdr.Name != "pkg/sub/new.py" || hdr.Mode != 0o644 || string(data) != "x = 1\n" {
				t.Fatalf("file entry %+v data %q", hdr, data)
			}
		} else if hdr.Typeflag != tar.TypeDir || hdr.Mode != 0o755 {
			t.Fatalf("unexpected entry %+v", hdr)
		}
	}
	if strings.Join(names, ",") != "pkg/,pkg/sub/,pkg/sub/new.py" {
		t.Fatalf("entries = %v", names)
	}
	if err := newTestManager(e).WriteFile(context.Background(), "0123456789abcdef", "big", make([]byte, MaxFileBytes+1)); !errors.Is(err, ErrTooLarge) {
		t.Fatalf("err = %v", err)
	}
}

func TestUploadTooLarge(t *testing.T) {
	e := newFakeEngine()
	m := newTestManager(e)
	m.maxUpload = 10
	err := m.Upload(context.Background(), "0123456789abcdef", bytes.NewReader(make([]byte, 11)))
	if !errors.Is(err, ErrTooLarge) {
		t.Fatalf("err = %v", err)
	}
}

func TestDestroyRemovesContainerAndVolume(t *testing.T) {
	e := newFakeEngine()
	if err := newTestManager(e).Destroy(context.Background(), "0123456789abcdef"); err != nil {
		t.Fatal(err)
	}
	if len(e.removedCtrs) != 1 || e.removedCtrs[0] != "rp-sbx-0123456789abcdef" ||
		len(e.removedVols) != 1 || e.removedVols[0] != "rp-ws-0123456789abcdef" {
		t.Fatalf("removed %v / %v", e.removedCtrs, e.removedVols)
	}
}

func TestReaperRemovesExpiredOnly(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	e := newFakeEngine()
	e.listed = []docker.ContainerSummary{
		{ID: "c-old", Names: []string{"/rp-sbx-00000000000000aa"},
			Labels: map[string]string{"repopilot.sandbox": "1", "repopilot.created": strconv.FormatInt(now.Add(-3*time.Hour).Unix(), 10)}},
		{ID: "c-new", Names: []string{"/rp-sbx-00000000000000bb"},
			Labels: map[string]string{"repopilot.sandbox": "1", "repopilot.created": strconv.FormatInt(now.Add(-10*time.Minute).Unix(), 10)}},
	}
	m := newTestManager(e)
	m.cfg.Now = func() time.Time { return now }
	removed, err := m.Reap(context.Background())
	if err != nil || removed != 1 {
		t.Fatalf("removed = %d err = %v", removed, err)
	}
	if len(e.removedCtrs) != 1 || e.removedCtrs[0] != "rp-sbx-00000000000000aa" ||
		len(e.removedVols) != 1 || e.removedVols[0] != "rp-ws-00000000000000aa" {
		t.Fatalf("removed %v / %v", e.removedCtrs, e.removedVols)
	}
}
