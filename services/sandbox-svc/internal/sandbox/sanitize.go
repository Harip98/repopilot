package sandbox

import (
	"archive/tar"
	"fmt"
	"io"
	"path"
	"strings"
)

type sanitizeOpts struct {
	strip   int  // leading path components to drop (1 for a daemon snapshot of /workspace)
	skipBad bool // drop disallowed entries instead of failing (restoring our own snapshot)
}

// sanitizeTar copies a tar stream, keeping only what may land in a workspace. Docker extracts
// archives as root and follows symlinks created earlier in the same archive, so a raw upload
// could write outside the volume (e.g. a link "evil -> /etc" then a file "evil/x"). Every
// entry is re-written: names are workspace-relative, types are regular file, directory or
// symlink only, special mode bits are cleared, ownership is the sandbox user, nothing is
// written beneath a symlink from the same archive, and every parent directory is emitted
// explicitly so an existing symlink in the workspace is replaced rather than followed.
func sanitizeTar(r io.Reader, w io.Writer, opts sanitizeOpts) error {
	tr := tar.NewReader(r)
	tw := tar.NewWriter(w)
	dirs := map[string]bool{}
	links := map[string]bool{}
	reject := func(name, why string) error {
		if opts.skipBad {
			return nil
		}
		return fmt.Errorf("%w: %q: %s", ErrBadArchive, name, why)
	}
	writeDir := func(name string, mode int64, h *tar.Header) error {
		dirs[name] = true
		return tw.WriteHeader(&tar.Header{Name: name + "/", Typeflag: tar.TypeDir,
			Mode: mode&0o777 | 0o700, Uid: sandboxUID, Gid: sandboxUID, ModTime: h.ModTime})
	}
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return tw.Close()
		}
		if err != nil {
			return fmt.Errorf("%w: %v", ErrBadArchive, err)
		}
		name := strings.TrimPrefix(h.Name, "./")
		if opts.strip > 0 {
			parts := strings.SplitN(name, "/", opts.strip+1)
			if len(parts) <= opts.strip {
				continue // the stripped directory itself
			}
			name = parts[opts.strip]
		}
		name = strings.TrimSuffix(name, "/")
		if name == "" || name == "." {
			continue
		}
		clean, err := CleanRelPath(name)
		if err != nil {
			if err := reject(h.Name, "name leaves the workspace"); err != nil {
				return err
			}
			continue
		}
		underLink := false
		for p := path.Dir(clean); p != "."; p = path.Dir(p) {
			underLink = underLink || links[p]
		}
		if underLink || (links[clean] && h.Typeflag == tar.TypeDir) {
			if err := reject(h.Name, "path goes through a symlink"); err != nil {
				return err
			}
			continue
		}
		switch h.Typeflag {
		case tar.TypeReg, tar.TypeDir, tar.TypeSymlink:
		default:
			if err := reject(h.Name, fmt.Sprintf("entry type %q not allowed", h.Typeflag)); err != nil {
				return err
			}
			continue
		}
		var parents []string
		for p := path.Dir(clean); p != "." && !dirs[p]; p = path.Dir(p) {
			parents = append([]string{p}, parents...)
		}
		for _, p := range parents {
			if err := writeDir(p, 0o755, h); err != nil {
				return err
			}
		}
		switch h.Typeflag {
		case tar.TypeDir:
			if !dirs[clean] {
				if err := writeDir(clean, h.Mode, h); err != nil {
					return err
				}
			}
		case tar.TypeSymlink:
			links[clean] = true
			if err := tw.WriteHeader(&tar.Header{Name: clean, Typeflag: tar.TypeSymlink,
				Linkname: h.Linkname, Mode: 0o777, Uid: sandboxUID, Gid: sandboxUID,
				ModTime: h.ModTime}); err != nil {
				return err
			}
		case tar.TypeReg:
			delete(links, clean)
			if err := tw.WriteHeader(&tar.Header{Name: clean, Typeflag: tar.TypeReg,
				Mode: h.Mode&0o777 | 0o600, Size: h.Size, Uid: sandboxUID, Gid: sandboxUID,
				ModTime: h.ModTime}); err != nil {
				return err
			}
			if _, err := io.Copy(tw, tr); err != nil {
				return fmt.Errorf("%w: %v", ErrBadArchive, err)
			}
		}
	}
}
