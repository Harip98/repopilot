package sandbox

import (
	"errors"
	"testing"
)

func TestCleanRelPath(t *testing.T) {
	tests := []struct {
		in, want string
		err      error
	}{
		{"a/b.py", "a/b.py", nil},
		{"./a//b.py", "a/b.py", nil},
		{"a/./b/../c.py", "a/c.py", nil},
		{"", "", ErrBadPath},
		{".", "", ErrBadPath},
		{"/etc/passwd", "", ErrBadPath},
		{"../x", "", ErrBadPath},
		{"a/../../x", "", ErrBadPath},
		{`a\b`, "", ErrBadPath},
		{"a\x00b", "", ErrBadPath},
	}
	for _, tc := range tests {
		got, err := CleanRelPath(tc.in)
		if !errors.Is(err, tc.err) || got != tc.want {
			t.Errorf("CleanRelPath(%q) = %q, %v; want %q, %v", tc.in, got, err, tc.want, tc.err)
		}
	}
}

func TestValidateArgv(t *testing.T) {
	long := make([]string, 65)
	for i := range long {
		long[i] = "x"
	}
	long[0] = "python"
	big := []string{"python", string(make([]byte, 33*1024))}
	tests := []struct {
		name string
		argv []string
		err  error
	}{
		{"allowed", []string{"pytest", "-q"}, nil},
		{"shell", []string{"sh", "-c", "id"}, ErrCommandNotAllowed},
		{"absolute path", []string{"/usr/bin/python", "-V"}, ErrCommandNotAllowed},
		{"empty", nil, ErrBadArgv},
		{"too many args", long, ErrBadArgv},
		{"too many bytes", big, ErrBadArgv},
	}
	for _, tc := range tests {
		if err := validateArgv(tc.argv); !errors.Is(err, tc.err) {
			t.Errorf("%s: err = %v, want %v", tc.name, err, tc.err)
		}
	}
}

func TestClampTimeout(t *testing.T) {
	for in, want := range map[int]int{0: 120, -5: 120, 1: 1, 300: 300, 600: 600, 9999: 600} {
		if got := clampTimeout(in); got != want {
			t.Errorf("clampTimeout(%d) = %d, want %d", in, got, want)
		}
	}
}
