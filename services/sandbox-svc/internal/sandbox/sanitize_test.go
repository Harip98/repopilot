package sandbox

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"strings"
	"testing"
)

type entry struct {
	hdr  tar.Header
	data string
}

func buildTar(t *testing.T, entries ...entry) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, e := range entries {
		h := e.hdr
		if h.Typeflag == tar.TypeReg {
			h.Size = int64(len(e.data))
		}
		if err := tw.WriteHeader(&h); err != nil {
			t.Fatal(err)
		}
		if e.data != "" {
			if _, err := io.WriteString(tw, e.data); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func sanitizeToHeaders(t *testing.T, in []byte, strip int, skipBad ...bool) ([]*tar.Header, map[string]string, error) {
	t.Helper()
	var out bytes.Buffer
	if err := sanitizeTar(bytes.NewReader(in), &out, sanitizeOpts{strip: strip, skipBad: len(skipBad) > 0 && skipBad[0]}); err != nil {
		return nil, nil, err
	}
	tr := tar.NewReader(&out)
	var hdrs []*tar.Header
	data := map[string]string{}
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return hdrs, data, nil
		}
		if err != nil {
			t.Fatal(err)
		}
		b, _ := io.ReadAll(tr)
		hdrs = append(hdrs, h)
		data[h.Name] = string(b)
	}
}

func reg(name, data string, mode int64) entry {
	return entry{tar.Header{Name: name, Typeflag: tar.TypeReg, Mode: mode}, data}
}

func TestSanitizeNormalisesAndAddsParents(t *testing.T) {
	in := buildTar(t,
		reg("./pkg/sub/a.py", "x = 1\n", 0o4755), // setuid stripped
		entry{tar.Header{Name: "pkg/link", Typeflag: tar.TypeSymlink, Linkname: "/etc/passwd"}, ""},
		reg("README.md", "hi", 0o600),
	)
	hdrs, data, err := sanitizeToHeaders(t, in, 0)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, h := range hdrs {
		names = append(names, h.Name)
		if h.Uid != sandboxUID || h.Gid != sandboxUID {
			t.Errorf("%s owned by %d:%d", h.Name, h.Uid, h.Gid)
		}
		if h.Mode&^0o777 != 0 {
			t.Errorf("%s keeps special mode bits %o", h.Name, h.Mode)
		}
	}
	want := "pkg/,pkg/sub/,pkg/sub/a.py,pkg/link,README.md"
	if strings.Join(names, ",") != want {
		t.Fatalf("entries = %v, want %s", names, want)
	}
	if data["pkg/sub/a.py"] != "x = 1\n" || hdrs[2].Mode != 0o755 {
		t.Fatalf("file entry %+v data %q", hdrs[2], data["pkg/sub/a.py"])
	}
}

func TestSanitizeRejects(t *testing.T) {
	tests := map[string][]entry{
		"absolute name": {reg("/etc/pwned", "x", 0o644)},
		"dotdot name":   {reg("pkg/../../x", "x", 0o644)},
		"hardlink":      {{tar.Header{Name: "h", Typeflag: tar.TypeLink, Linkname: "../../etc/shadow"}, ""}},
		"char device":   {{tar.Header{Name: "mem", Typeflag: tar.TypeChar, Devmajor: 1, Devminor: 1}, ""}},
		"block device":  {{tar.Header{Name: "sda", Typeflag: tar.TypeBlock, Devmajor: 8}, ""}},
		"fifo":          {{tar.Header{Name: "p", Typeflag: tar.TypeFifo}, ""}},
		"through symlink": {
			{tar.Header{Name: "evil", Typeflag: tar.TypeSymlink, Linkname: "/etc"}, ""},
			reg("evil/pwned", "x", 0o644),
		},
		"through nested symlink": {
			{tar.Header{Name: "a/b", Typeflag: tar.TypeSymlink, Linkname: "/usr/bin"}, ""},
			reg("a/b/c/timeout", "x", 0o755),
		},
	}
	for name, entries := range tests {
		t.Run(name, func(t *testing.T) {
			_, _, err := sanitizeToHeaders(t, buildTar(t, entries...), 0)
			if !errors.Is(err, ErrBadArchive) {
				t.Fatalf("err = %v, want ErrBadArchive", err)
			}
		})
	}
}

func TestSanitizeStripsLeadingComponent(t *testing.T) {
	// The daemon archives /workspace as "workspace/...": a reset restores it with strip=1.
	in := buildTar(t,
		entry{tar.Header{Name: "workspace/", Typeflag: tar.TypeDir, Mode: 0o755}, ""},
		reg("workspace/pkg/a.py", "x", 0o644),
	)
	hdrs, _, err := sanitizeToHeaders(t, in, 1)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, h := range hdrs {
		names = append(names, h.Name)
	}
	if strings.Join(names, ",") != "pkg/,pkg/a.py" {
		t.Fatalf("entries = %v", names)
	}
}

func TestSanitizeSkipBadDropsEntries(t *testing.T) {
	in := buildTar(t,
		reg("ok.py", "x", 0o644),
		entry{tar.Header{Name: "h", Typeflag: tar.TypeLink, Linkname: "ok.py"}, ""},
		reg("/abs", "x", 0o644),
		reg("also_ok.py", "y", 0o644),
	)
	hdrs, _, err := sanitizeToHeaders(t, in, 0, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(hdrs) != 2 || hdrs[0].Name != "ok.py" || hdrs[1].Name != "also_ok.py" {
		t.Fatalf("entries = %+v", hdrs)
	}
}
