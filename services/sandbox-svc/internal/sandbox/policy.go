package sandbox

import (
	"errors"
	"fmt"
	"path"
	"strings"

	"repopilot/sandbox-svc/internal/docker"
)

// Sentinel errors; the API layer maps each to an HTTP status and error code.
var (
	ErrImageNotAllowed   = errors.New("image not allowed")
	ErrCommandNotAllowed = errors.New("command not allowed")
	ErrBadArgv           = errors.New("bad argv")
	ErrBadPath           = errors.New("bad path")
	ErrNotAFile          = errors.New("not a regular file")
	ErrTooLarge          = errors.New("too large")
	ErrNotFound          = docker.ErrNotFound
)

const (
	MaxFileBytes      = 1 << 20   // single-file read/write cap
	MaxUploadBytes    = 256 << 20 // whole-repo upload cap
	maxArgs           = 64
	maxArgvBytes      = 32 << 10
	defaultTimeoutS   = 120
	maxTimeoutS       = 600
	workspaceDir      = "/workspace"
	sandboxUID        = 10001
	labelSandbox      = "repopilot.sandbox"
	labelCreated      = "repopilot.created"
	containerPrefix   = "rp-sbx-"
	volumePrefix      = "rp-ws-"
	killAllCmd        = "rp-killall" // baked into the base image; see bench/images/base
	killGraceSeconds  = "5"          // timeout -k: SIGKILL this long after SIGTERM
	workspaceTmpfsOpt = "size=512m,uid=10001,gid=10001"
)

// allowedCommands is defence in depth, not the boundary: the container is. It keeps the
// tool surface narrow (no shells) and makes every exec easy to audit.
var allowedCommands = map[string]bool{
	"git": true, "rg": true, "python": true, "python3": true, "pytest": true, "ruff": true,
	"ls": true, "cat": true, "head": true, "tail": true, "wc": true, "find": true, "diff": true,
}

// CleanRelPath normalises a workspace-relative path and rejects anything that could name a
// location outside the workspace.
func CleanRelPath(p string) (string, error) {
	if p == "" || strings.ContainsAny(p, "\x00\\") || strings.HasPrefix(p, "/") {
		return "", fmt.Errorf("%w: %q", ErrBadPath, p)
	}
	clean := path.Clean(p)
	if clean == "." || clean == ".." || strings.HasPrefix(clean, "../") {
		return "", fmt.Errorf("%w: %q", ErrBadPath, p)
	}
	return clean, nil
}

func validateArgv(argv []string) error {
	if len(argv) == 0 || len(argv) > maxArgs {
		return fmt.Errorf("%w: %d arguments (1..%d)", ErrBadArgv, len(argv), maxArgs)
	}
	total := 0
	for _, a := range argv {
		total += len(a)
	}
	if total > maxArgvBytes {
		return fmt.Errorf("%w: %d bytes (max %d)", ErrBadArgv, total, maxArgvBytes)
	}
	if !allowedCommands[argv[0]] {
		return fmt.Errorf("%w: %q", ErrCommandNotAllowed, argv[0])
	}
	return nil
}

// ClampTimeout applies the exec timeout default (120s) and ceiling (600s).
func ClampTimeout(s int) int {
	switch {
	case s <= 0:
		return defaultTimeoutS
	case s > maxTimeoutS:
		return maxTimeoutS
	default:
		return s
	}
}
