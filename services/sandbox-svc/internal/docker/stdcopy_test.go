package docker

import (
	"bytes"
	"encoding/binary"
	"strings"
	"testing"
)

func frame(stream byte, payload []byte) []byte {
	hdr := make([]byte, 8)
	hdr[0] = stream
	binary.BigEndian.PutUint32(hdr[4:], uint32(len(payload)))
	return append(hdr, payload...)
}

func TestDemuxSplitsStreams(t *testing.T) {
	var in bytes.Buffer
	in.Write(frame(1, []byte("out-1 ")))
	in.Write(frame(2, []byte("err-1")))
	in.Write(frame(1, []byte("out-2")))
	stdout, stderr := &CappedBuffer{Max: 1024}, &CappedBuffer{Max: 1024}
	if err := Demux(&in, stdout, stderr); err != nil {
		t.Fatal(err)
	}
	if stdout.String() != "out-1 out-2" || stderr.String() != "err-1" {
		t.Fatalf("stdout=%q stderr=%q", stdout.String(), stderr.String())
	}
	if stdout.Truncated() || stderr.Truncated() {
		t.Fatal("unexpected truncation")
	}
}

func TestDemuxCapsAndDrains(t *testing.T) {
	var in bytes.Buffer
	chunk := bytes.Repeat([]byte("x"), 10*1024)
	for range 20 { // 200 KiB of stdout
		in.Write(frame(1, chunk))
	}
	in.Write(frame(2, []byte("tail")))
	stdout, stderr := &CappedBuffer{Max: 64 * 1024}, &CappedBuffer{Max: 64 * 1024}
	if err := Demux(&in, stdout, stderr); err != nil {
		t.Fatal(err)
	}
	if got := len(stdout.String()); got != 64*1024 {
		t.Fatalf("stdout len = %d, want %d", got, 64*1024)
	}
	if !stdout.Truncated() {
		t.Fatal("stdout should be truncated")
	}
	if stderr.String() != "tail" {
		t.Fatalf("stderr after the cap was not read: %q", stderr.String())
	}
	if in.Len() != 0 {
		t.Fatalf("%d bytes left unread", in.Len())
	}
}

func TestCappedBufferValidUTF8(t *testing.T) {
	b := &CappedBuffer{Max: 100}
	_, _ = b.Write([]byte{'o', 'k', 0xff, 0xfe})
	if got := b.String(); got != "ok�" || !strings.HasPrefix(got, "ok") {
		t.Fatalf("got %q", got)
	}
}
