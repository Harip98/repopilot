// Command sandbox-svc owns the Docker socket and runs agent commands in locked-down containers.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"repopilot/sandbox-svc/internal/api"
)

const defaultAddr = "127.0.0.1:8090"

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// healthURL is the loopback /healthz URL for a listen address such as ":8090" or "0.0.0.0:8090".
func healthURL(addr string) string {
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		port = "8090"
	}
	return "http://127.0.0.1:" + port + "/healthz"
}

// healthcheck backs the container HEALTHCHECK: the distroless image has no shell or curl.
func healthcheck(url string) int {
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "-healthcheck" {
		os.Exit(healthcheck(healthURL(getenv("SANDBOX_ADDR", defaultAddr))))
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(logger); err != nil {
		logger.Error("sandbox-svc stopped", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	addr := getenv("SANDBOX_ADDR", defaultAddr)
	srv := &http.Server{
		Addr:              addr,
		Handler:           api.NewServer(logger),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      30 * time.Second,
	}
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	logger.Info("sandbox-svc listening", "addr", addr)
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdown); err != nil && !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	}
}
