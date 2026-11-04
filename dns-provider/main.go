// Fabricator's private, ExternalDNS-compatible DNS management service.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func main() {
	cfg, err := loadConfig()
	if err != nil {
		slog.Error("configuration", "error", err)
		os.Exit(1)
	}
	var backend Backend
	switch cfg.Provider {
	case "cloudflare":
		backend = newCloudflare(os.Getenv("CLOUDFLARE_API_TOKEN"))
	case "powerdns":
		backend = newPowerDNS(os.Getenv("POWERDNS_API_URL"), os.Getenv("POWERDNS_API_KEY"), env("POWERDNS_SERVER_ID", "localhost"))
	}
	service := &Service{backend: backend, zones: cfg.Zones, domains: cfg.Domains}
	server := &http.Server{Addr: cfg.Listen, Handler: service.Handler(), ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 30 * time.Second, WriteTimeout: 120 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	slog.Info("DNS webhook listening", "address", cfg.Listen, "provider", cfg.Provider)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("HTTP server", "error", err)
		os.Exit(1)
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

type Config struct {
	Provider, Listen string
	Zones, Domains   []string
}

func loadConfig() (Config, error) {
	c := Config{Provider: os.Getenv("DNS_PROVIDER"), Listen: env("DNS_LISTEN", "127.0.0.1:8888")}
	for _, z := range strings.Split(os.Getenv("DNS_ZONES"), ",") {
		z = canonicalName(strings.TrimSpace(z))
		if !validName(z, false) {
			return c, errors.New("DNS_ZONES must contain explicit authoritative zone names")
		}
		c.Zones = append(c.Zones, z)
	}
	for _, d := range strings.Split(env("DNS_DOMAIN_FILTER", strings.Join(c.Zones, ",")), ",") {
		d = canonicalName(strings.TrimSpace(d))
		if !validName(d, false) || zoneFor(d, c.Zones) == "" {
			return c, errors.New("DNS_DOMAIN_FILTER must contain subtrees within DNS_ZONES")
		}
		c.Domains = append(c.Domains, d)
	}
	switch c.Provider {
	case "cloudflare":
		if os.Getenv("CLOUDFLARE_API_TOKEN") == "" {
			return c, errors.New("CLOUDFLARE_API_TOKEN required")
		}
	case "powerdns":
		if err := validatePowerDNSURL(os.Getenv("POWERDNS_API_URL")); err != nil {
			return c, err
		}
		if os.Getenv("POWERDNS_API_KEY") == "" {
			return c, errors.New("POWERDNS_API_KEY required")
		}
	default:
		return c, errors.New("DNS_PROVIDER must be cloudflare or powerdns")
	}
	return c, nil
}
