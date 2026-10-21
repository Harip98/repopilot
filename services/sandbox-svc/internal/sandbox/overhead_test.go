package sandbox

import (
	"strconv"
	"testing"
	"time"
)

// The API extends an exec's write deadline by ExecOverhead; it must cover everything Exec can
// spend beyond the command's own timeout, or a slow cleanup drops the connection.
func TestExecOverheadCoversWorstCase(t *testing.T) {
	grace, _ := strconv.Atoi(killGraceSeconds)
	m := NewManager(nil, Config{}, nil)
	worst := time.Duration(grace)*time.Second + m.backstopGrace + cleanupTimeout + resetTimeout
	if ExecOverhead < worst {
		t.Fatalf("ExecOverhead = %v, worst case = %v", ExecOverhead, worst)
	}
}
