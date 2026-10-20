// Package sandbox creates, drives and destroys hardened per-run containers.
package sandbox

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"path"
	"strconv"
	"strings"
	"time"

	"repopilot/sandbox-svc/internal/docker"
)

// Engine is the subset of the Docker client the manager needs.
type Engine interface {
	CreateVolume(ctx context.Context, name string, driverOpts, labels map[string]string) error
	RemoveVolume(ctx context.Context, name string) error
	CreateContainer(ctx context.Context, name string, spec docker.ContainerSpec) (string, error)
	StartContainer(ctx context.Context, id string) error
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
}

func NewManager(e Engine, cfg Config, logger *slog.Logger) *Manager {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Manager{e: e, cfg: cfg, log: logger, backstopGrace: 15 * time.Second, maxUpload: MaxUploadBytes}
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

// Upload extracts a tar stream into the workspace.
func (m *Manager) Upload(ctx context.Context, id string, r io.Reader) error {
	lr := &limitedReader{r: r, max: m.maxUpload}
	err := m.e.PutArchive(ctx, containerName(id), workspaceDir, lr)
	if lr.n > lr.max {
		return fmt.Errorf("%w: upload exceeds %d bytes", ErrTooLarge, lr.max)
	}
	return err
}

// ReadFile returns one regular file from the workspace.
func (m *Manager) ReadFile(ctx context.Context, id, p string) ([]byte, error) {
	clean, err := CleanRelPath(p)
	if err != nil {
		return nil, err
	}
	rc, err := m.e.GetArchive(ctx, containerName(id), workspaceDir+"/"+clean)
	if err != nil {
		return nil, err
	}
	defer rc.Close()
	tr := tar.NewReader(rc)
	hdr, err := tr.Next()
	if err != nil {
		return nil, fmt.Errorf("read archive: %w", err)
	}
	// The daemon archives a symlink as the link itself, so this also refuses to follow links.
	if hdr.Typeflag != tar.TypeReg {
		return nil, fmt.Errorf("%w: %q", ErrNotAFile, clean)
	}
	if hdr.Size > MaxFileBytes {
		return nil, fmt.Errorf("%w: %q is %d bytes (max %d)", ErrTooLarge, clean, hdr.Size, MaxFileBytes)
	}
	return io.ReadAll(tr)
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
	return m.e.PutArchive(ctx, containerName(id), workspaceDir, &buf)
}

// Exec runs an allowlisted command under coreutils timeout. If the output stream outlives
// the deadline (a background child holding stdout open), the stream is abandoned and every
// process except init and the container's main process is killed; the workspace survives.
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

	deadline := time.Duration(timeout)*time.Second + m.backstopGrace
	execCtx, cancel := context.WithTimeout(ctx, deadline)
	defer cancel()
	start := m.cfg.Now()
	res, err := m.e.Exec(execCtx, containerName(id), argv, workdir, m.cfg.MaxOutput)
	elapsed := m.cfg.Now().Sub(start)
	if err != nil {
		if errors.Is(execCtx.Err(), context.DeadlineExceeded) && ctx.Err() == nil {
			m.killAll(id)
			return ExecResponse{
				ExitCode: -1, TimedOut: true, DurationMs: elapsed.Milliseconds(),
				Stderr: "repopilot: output stream outlived the timeout; sandbox processes were killed",
			}, nil
		}
		return ExecResponse{}, err
	}
	timedOut := res.ExitCode == 124 || (res.ExitCode == 137 && elapsed >= time.Duration(timeout)*time.Second)
	return ExecResponse{
		ExitCode: res.ExitCode, Stdout: res.Stdout, Stderr: res.Stderr,
		StdoutTruncated: res.StdoutTruncated, StderrTruncated: res.StderrTruncated,
		TimedOut: timedOut, DurationMs: elapsed.Milliseconds(),
	}, nil
}

func (m *Manager) killAll(id string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if _, err := m.e.Exec(ctx, containerName(id), []string{killAllCmd}, workspaceDir, 4096); err != nil {
		m.log.Warn("backstop kill failed", "id", id, "err", err)
	}
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
