package docker

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"strings"
)

// CappedBuffer keeps the first Max bytes written to it and silently discards the rest, so a
// noisy command can never grow the service's memory or its responses.
type CappedBuffer struct {
	Max       int
	buf       []byte
	truncated bool
}

func (b *CappedBuffer) Write(p []byte) (int, error) {
	room := b.Max - len(b.buf)
	if room <= 0 {
		if len(p) > 0 {
			b.truncated = true
		}
		return len(p), nil
	}
	if len(p) > room {
		b.buf = append(b.buf, p[:room]...)
		b.truncated = true
		return len(p), nil
	}
	b.buf = append(b.buf, p...)
	return len(p), nil
}

// String returns the kept bytes as valid UTF-8, replacing invalid sequences with U+FFFD.
func (b *CappedBuffer) String() string { return strings.ToValidUTF8(string(b.buf), "�") }

func (b *CappedBuffer) Truncated() bool { return b.truncated }

// Demux splits Docker's multiplexed exec stream (8-byte header: stream id, 3 zero bytes,
// big-endian payload size) into stdout and stderr. It reads r to EOF.
func Demux(r io.Reader, stdout, stderr io.Writer) error {
	hdr := make([]byte, 8)
	for {
		if _, err := io.ReadFull(r, hdr); err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return fmt.Errorf("read frame header: %w", err)
		}
		dst := stdout
		if hdr[0] == 2 {
			dst = stderr
		}
		size := int64(binary.BigEndian.Uint32(hdr[4:]))
		if _, err := io.CopyN(dst, r, size); err != nil {
			return fmt.Errorf("read frame payload: %w", err)
		}
	}
}
