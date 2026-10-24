// Package sandbox creates, drives and destroys hardened per-run containers.
package sandbox

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"

	"repopilot/sandbox-svc/internal/docker"
)

// Engine is the subset of the Docker client the manager needs.
type Engine interface {
	CreateVolume(ctx context.Context, name string, driverOpts, labels map[string]string) error
	RemoveVolume(ctx context.Context, name string) error
	CreateContainer(ctx context.Context, name string, spec docker.ContainerSpec) (string, error)
	StartContainer(ctx context.Context, id string) error
	RestartContainer(ctx context.Context, id string) error
	RemoveContainer(ctx context.Context, id string) error
	ListContainers(ctx context.Context, label string) ([]docker.ContainerSummary, error)
	PutArchive(ctx context.Context, id, dir string, tar io.Reader) error
	GetArchive(ctx context.Context, id, path string) (io.ReadCloser, error)
	Exec(ctx context.Context, id string, argv []string, workdir string, maxOut int) (docker.ExecResult, error)
}

type Config struct {
	ImagePrefixes []string
	TTL           time.Duration
	MaxOutput     int
	Now           func() time.Time
}

type ExecRequest struct {
	Argv     []string `json:"argv"`
	TimeoutS int      `json:"timeout_s"`
	Cwd      string   `json:"cwd"`
}

type ExecResponse struct {
	ExitCode        int    `json:"exit_code"`
	Stdout          string `json:"stdout"`
	Stderr          string `json:"stderr"`
	StdoutTruncated bool   `json:"stdout_truncated"`
	StderrTruncated bool   `json:"stderr_truncated"`
	TimedOut        bool   `json:"timed_out"`
	DurationMs      int64  `json:"duration_ms"`
}

// Manager keeps no in-memory registry: a sandbox id maps to fixed container and volume
// names, so sandboxes survive a sandbox-svc restart and the reaper can find orphans.
type Manager struct {
	e             Engine
	cfg           Config
	log           *slog.Logger
	backstopGrace time.Duration // beyond timeout+kill grace before the stream is abandoned
	maxUpload     int64
	locksMu       sync.Mutex
	locks         map[string]*sandboxLock // one exec/read/reset at a time; see lock
}

// sandboxLock is one sandbox's mutex and the number of callers holding or awaiting it.
type sandboxLock struct {
	mu   sync.Mutex
	refs int
}

// lock serialises work on one sandbox: the post-exec cleanup kills every process, so a
// concurrent exec in the same sandbox would be killed with it. An entry lives only while a
// caller holds or awaits it, so a destroyed or reaped sandbox leaves none behind. Destroy does
// not drop it itself: a caller still in flight would then hold a mutex a newcomer never sees.
func (m *Manager) lock(id string) func() {
	m.locksMu.Lock()
	l := m.locks[id]
	if l == nil {
		l = &sandboxLock{}
		m.locks[id] = l
	}
	l.refs++
	m.locksMu.Unlock()
	l.mu.Lock()
	return func() {
		l.mu.Unlock()
		m.locksMu.Lock()
		if l.refs--; l.refs == 0 {
			delete(m.locks, id)
		}
		m.locksMu.Unlock()
	}
}

func NewManager(e Engine, cfg Config, logger *slog.Logger) *Manager {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Manager{e: e, cfg: cfg, log: logger, backstopGrace: 15 * time.Second, maxUpload: MaxUploadBytes,
		locks: map[string]*sandboxLock{}}
}

func containerName(id string) string { return containerPrefix + id }
func volumeName(id string) string    { return volumePrefix + id }

func (m *Manager) imageAllowed(image string) bool {
	for _, p := range m.cfg.ImagePrefixes {
		if p != "" && strings.HasPrefix(image, p) {
			return true
		}
	}
	return false
}

// Create starts a new sandbox from an allowlisted image and returns its id.
func (m *Manager) Create(ctx context.Context, image string) (string, error) {
	if !m.imageAllowed(image) {
		return "", fmt.Errorf("%w: %q", ErrImageNotAllowed, image)
	}
	raw := make([]byte, 8)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	id := hex.EncodeToString(raw)
	labels := map[string]string{
		labelSandbox: "1",
		labelCreated: strconv.FormatInt(m.cfg.Now().Unix(), 10),
	}
	opts := map[string]string{"type": "tmpfs", "device": "tmpfs", "o": workspaceTmpfsOpt}
	if err := m.e.CreateVolume(ctx, volumeName(id), opts, labels); err != nil {
		return "", fmt.Errorf("create volume: %w", err)
	}
	spec := docker.ContainerSpec{
		Image:      image,
		User:       fmt.Sprintf("%d:%d", sandboxUID, sandboxUID),
		Env:        []string{"HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"},
		WorkingDir: workspaceDir,
		Cmd:        []string{"sleep", "infinity"},
		Labels:     labels,
		Host: docker.HostConfig{
			NetworkMode:    "none",
			CapDrop:        []string{"ALL"},
			SecurityOpt:    []string{"no-new-privileges:true"},
			ReadonlyRootfs: true,
			Init:           true,
			IpcMode:        "private",
			Memory:         1 << 30,
			MemorySwap:     1 << 30, // equal to Memory: no swap
			NanoCpus:       1e9,
			PidsLimit:      256,
			Tmpfs:          map[string]string{"/tmp": "rw,nosuid,nodev,size=256m"},
			Mounts:         []docker.Mount{{Type: "volume", Source: volumeName(id), Target: workspaceDir}},
		},
	}
	if _, err := m.e.CreateContainer(ctx, containerName(id), spec); err != nil {
		m.cleanup(id)
		return "", fmt.Errorf("create container: %w", err)
	}
	if err := m.e.StartContainer(ctx, containerName(id)); err != nil {
		m.cleanup(id)
		return "", fmt.Errorf("start container: %w", err)
	}
	m.log.Info("sandbox created", "id", id, "image", image)
	return id, nil
}

// cleanup removes a half-built sandbox; errors are logged, not returned.
func (m *Manager) cleanup(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := m.e.RemoveContainer(ctx, containerName(id)); err != nil && !errors.Is(err, docker.ErrNotFound) {
		m.log.Warn("cleanup container", "id", id, "err", err)
	}
	if err := m.e.RemoveVolume(ctx, volumeName(id)); err != nil && !errors.Is(err, docker.ErrNotFound) {
		m.log.Warn("cleanup volume", "id", id, "err", err)
	}
}

// limitedReader fails once more than max bytes have been read, so an oversized upload is
// rejected instead of silently truncated.
type limitedReader struct {
	r   io.Reader
	n   int64
	max int64
}

func (l *limitedReader) Read(p []byte) (int, error) {
	n, err := l.r.Read(p)
	l.n += int64(n)
	if l.n > l.max {
		return n, fmt.Errorf("%w: upload exceeds %d bytes", ErrTooLarge, l.max)
	}
	return n, err
}

// Upload extracts a tar stream into the workspace. The stream is sanitised into a temp file
// first, so a malicious archive is rejected before anything is written.
func (m *Manager) Upload(ctx context.Context, id string, r io.Reader) error {
	tmp, err := os.CreateTemp("", "rp-upload-*.tar")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()
	lr := &limitedReader{r: r, max: m.maxUpload}
	err = sanitizeTar(lr, tmp, sanitizeOpts{})
	if lr.n > lr.max {
		return fmt.Errorf("%w: upload exceeds %d bytes", ErrTooLarge, lr.max)
	}
	if err != nil {
		return err
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		return err
	}
	defer m.lock(id)()
	return m.e.PutArchive(ctx, containerName(id), workspaceDir, tmp)
}

// ReadFile returns one regular file from the workspace. It runs rp-read inside the sandbox
// as the sandbox user, which opens each path component with O_NOFOLLOW: a symlink anywhere
// in the path is refused, and nothing is read with more privilege than the agent has.
func (m *Manager) ReadFile(ctx context.Context, id, p string) ([]byte, error) {
	clean, err := CleanRelPath(p)
	if err != nil {
		return nil, err
	}
	defer m.lock(id)()
	res, err := m.e.Exec(ctx, containerName(id), []string{"timeout", "-k", "5", "30s", readCmd, clean},
		workspaceDir, base64.StdEncoding.EncodedLen(MaxFileBytes)+1024)
	if err != nil {
		return nil, err
	}
	switch res.ExitCode {
	case 0:
		return base64.StdEncoding.DecodeString(strings.TrimSpace(res.Stdout))
	case 3:
		return nil, fmt.Errorf("%w: %q", ErrNotAFile, clean)
	case 4:
		return nil, fmt.Errorf("%w: %q", ErrNotFound, clean)
	case 5:
		return nil, fmt.Errorf("%w: %q exceeds %d bytes", ErrTooLarge, clean, MaxFileBytes)
	default:
		return nil, fmt.Errorf("read %q: exit %d: %s", clean, res.ExitCode, res.Stderr)
	}
}

// WriteFile creates or replaces one file in the workspace, creating parent directories.
func (m *Manager) WriteFile(ctx context.Context, id, p string, data []byte) error {
	clean, err := CleanRelPath(p)
	if err != nil {
		return err
	}
	if len(data) > MaxFileBytes {
		return fmt.Errorf("%w: %d bytes (max %d)", ErrTooLarge, len(data), MaxFileBytes)
	}
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	var parents []string
	for dir := path.Dir(clean); dir != "."; dir = path.Dir(dir) {
		parents = append([]string{dir}, parents...)
	}
	now := m.cfg.Now()
	for _, dir := range parents {
		if err := tw.WriteHeader(&tar.Header{Name: dir + "/", Typeflag: tar.TypeDir, Mode: 0o755,
			Uid: sandboxUID, Gid: sandboxUID, ModTime: now}); err != nil {
			return err
		}
	}
	if err := tw.WriteHeader(&tar.Header{Name: clean, Typeflag: tar.TypeReg, Mode: 0o644,
		Size: int64(len(data)), Uid: sandboxUID, Gid: sandboxUID, ModTime: now}); err != nil {
		return err
	}
	if _, err := tw.Write(data); err != nil {
		return err
	}
	if err := tw.Close(); err != nil {
		return err
	}
	defer m.lock(id)()
	return m.e.PutArchive(ctx, containerName(id), workspaceDir, &buf)
}

// Exec runs an allowlisted command under coreutils timeout, then kills every process the
// command left behind (children that escaped timeout's process group). If that cleanup cannot
// even start, the sandbox has run out of PIDs: it is reset with its workspace preserved.
// When the output stream outlives the deadline, the stream is abandoned and the same cleanup
// runs; the workspace survives either way.
func (m *Manager) Exec(ctx context.Context, id string, req ExecRequest) (ExecResponse, error) {
	if err := validateArgv(req.Argv); err != nil {
		return ExecResponse{}, err
	}
	workdir := workspaceDir
	if req.Cwd != "" {
		clean, err := CleanRelPath(req.Cwd)
		if err != nil {
			return ExecResponse{}, err
		}
		workdir += "/" + clean
	}
	timeout := ClampTimeout(req.TimeoutS)
	argv := append([]string{"timeout", "-k", killGraceSeconds, strconv.Itoa(timeout) + "s"}, req.Argv...)

	defer m.lock(id)()
	deadline := time.Duration(timeout)*time.Second + m.backstopGrace
	execCtx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	start := m.cfg.Now()
	res, err := m.e.Exec(execCtx, containerName(id), argv, workdir, m.cfg.MaxOutput)
	elapsed := m.cfg.Now().Sub(start)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return ExecResponse{}, err
		}
		backstop := errors.Is(execCtx.Err(), context.DeadlineExceeded) && ctx.Err() == nil
		m.cleanUp(id)
		if backstop {
			return ExecResponse{
				ExitCode: -1, TimedOut: true, DurationMs: elapsed.Milliseconds(),
				Stderr: "repopilot: output stream outlived the timeout; sandbox processes were killed",
			}, nil
		}
		return ExecResponse{}, err
	}
	m.cleanUp(id)
	if res.ExitCode == 125 && strings.Contains(res.Stderr, "fork system call failed") {
		return ExecResponse{}, fmt.Errorf("%w: no free processes to start %q; leftover processes "+
			"were cleared (workspace kept), retry the command", ErrSandboxExhausted, req.Argv[0])
	}
	timedOut := res.ExitCode == 124 || (res.ExitCode == 137 && elapsed >= time.Duration(timeout)*time.Second)
	return ExecResponse{
		ExitCode: res.ExitCode, Stdout: res.Stdout, Stderr: res.Stderr,
		StdoutTruncated: res.StdoutTruncated, StderrTruncated: res.StderrTruncated,
		TimedOut: timedOut, DurationMs: elapsed.Milliseconds(),
	}, nil
}

// cleanUp kills leftover processes; when that is impossible it resets the sandbox. The caller
// holds the sandbox lock.
func (m *Manager) cleanUp(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), cleanupTimeout)
	defer cancel()
	res, err := m.e.Exec(ctx, containerName(id), []string{killAllCmd}, workspaceDir, 4096)
	if err == nil && res.ExitCode == 0 {
		return
	}
	m.log.Warn("process cleanup failed; resetting sandbox", "id", id, "err", err, "exit", res.ExitCode)
	if err := m.reset(id); err != nil {
		m.log.Error("sandbox reset failed", "id", id, "err", err)
	}
}

// reset restarts the container, which kills every process, and restores the workspace. The
// tmpfs workspace does not survive a restart, so it is snapshotted first through the archive
// API, which runs in the daemon and needs no free process inside the sandbox.
func (m *Manager) reset(id string) error {
	ctx, cancel := context.WithTimeout(context.Background(), resetTimeout)
	defer cancel()
	snap, err := os.CreateTemp("", "rp-reset-*.tar")
	if err != nil {
		return err
	}
	defer os.Remove(snap.Name())
	defer snap.Close()
	rc, err := m.e.GetArchive(ctx, containerName(id), workspaceDir)
	if err != nil {
		return fmt.Errorf("snapshot: %w", err)
	}
	err = sanitizeTar(rc, snap, sanitizeOpts{strip: 1, skipBad: true})
	rc.Close()
	if err != nil {
		return fmt.Errorf("snapshot: %w", err)
	}
	if err := m.e.RestartContainer(ctx, containerName(id)); err != nil {
		return fmt.Errorf("restart: %w", err)
	}
	if _, err := snap.Seek(0, io.SeekStart); err != nil {
		return err
	}
	if err := m.e.PutArchive(ctx, containerName(id), workspaceDir, snap); err != nil {
		return fmt.Errorf("restore: %w", err)
	}
	m.log.Info("sandbox reset", "id", id)
	return nil
}

// Destroy removes the sandbox container and its workspace volume.
func (m *Manager) Destroy(ctx context.Context, id string) error {
	errC := m.e.RemoveContainer(ctx, containerName(id))
	if errC != nil && !errors.Is(errC, docker.ErrNotFound) {
		return fmt.Errorf("remove container: %w", errC)
	}
	errV := m.e.RemoveVolume(ctx, volumeName(id))
	if errV != nil && !errors.Is(errV, docker.ErrNotFound) {
		return fmt.Errorf("remove volume: %w", errV)
	}
	if errC != nil && errV != nil {
		return ErrNotFound
	}
	m.log.Info("sandbox destroyed", "id", id)
	return nil
}

// Reap destroys sandboxes older than the TTL, including ones left by a previous process.
func (m *Manager) Reap(ctx context.Context) (int, error) {
	list, err := m.e.ListContainers(ctx, labelSandbox+"=1")
	if err != nil {
		return 0, err
	}
	removed := 0
	for _, c := range list {
		created, err := strconv.ParseInt(c.Labels[labelCreated], 10, 64)
		if err != nil || m.cfg.Now().Sub(time.Unix(created, 0)) <= m.cfg.TTL {
			continue
		}
		for _, name := range c.Names {
			id, ok := strings.CutPrefix(strings.TrimPrefix(name, "/"), containerPrefix)
			if !ok {
				continue
			}
			if err := m.Destroy(ctx, id); err != nil {
				m.log.Warn("reap", "id", id, "err", err)
				continue
			}
			removed++
		}
	}
	return removed, nil
}
