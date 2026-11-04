package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type powerDNS struct {
	base, apiKey, server string
	client               *http.Client
}
type pdnsRecord struct {
	Content  string `json:"content"`
	Disabled bool   `json:"disabled"`
}
type pdnsRRSet struct {
	Name       string       `json:"name"`
	Type       string       `json:"type"`
	TTL        uint32       `json:"ttl,omitempty"`
	ChangeType string       `json:"changetype,omitempty"`
	Records    []pdnsRecord `json:"records,omitempty"`
}

func validatePowerDNSURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "https" && u.Scheme != "http") {
		return errors.New("POWERDNS_API_URL must be an absolute http(s) URL without credentials, query, or fragment")
	}
	host := u.Hostname()
	ip := net.ParseIP(host)
	if u.Scheme == "http" && host != "localhost" && host != "powerdns" && (ip == nil || !(ip.IsLoopback() || ip.IsPrivate())) {
		return errors.New("use HTTPS for non-private PowerDNS API hosts")
	}
	return nil
}
func newPowerDNS(base, apiKey, server string) *powerDNS {
	return &powerDNS{base: strings.TrimRight(base, "/"), apiKey: apiKey, server: server,
		client: &http.Client{Timeout: 25 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
}
func (p *powerDNS) request(ctx context.Context, method, zone string, payload any, dst any) error {
	u := p.base + "/api/v1/servers/" + url.PathEscape(p.server) + "/zones/" + url.PathEscape(zone+".")
	var body io.Reader
	if payload != nil {
		data, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		body = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, u, body)
	if err != nil {
		return err
	}
	req.Header.Set("X-API-Key", p.apiKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := p.client.Do(req)
	if err != nil {
		return fmt.Errorf("PowerDNS request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("PowerDNS %s failed: HTTP %d", method, resp.StatusCode)
	}
	if dst != nil {
		if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(dst); err != nil {
			return fmt.Errorf("PowerDNS response: %w", err)
		}
	}
	return nil
}
func (p *powerDNS) List(ctx context.Context, zone string) ([]RRSet, error) {
	var data struct {
		RRSets []pdnsRRSet `json:"rrsets"`
	}
	if err := p.request(ctx, http.MethodGet, zone, nil, &data); err != nil {
		return nil, err
	}
	result := []RRSet{}
	for _, rr := range data.RRSets {
		if !supported[rr.Type] {
			continue
		}
		values := []string{}
		for _, record := range rr.Records {
			// A disabled value cannot be represented losslessly in ExternalDNS.
			// Refuse listing rather than silently deleting it in a later replacement.
			if record.Disabled {
				return nil, fmt.Errorf("disabled records in %s/%s cannot be safely managed", rr.Name, rr.Type)
			}
			values = append(values, record.Content)
		}
		if len(values) > 0 {
			result = append(result, RRSet{Name: rr.Name, Type: rr.Type, TTL: rr.TTL, Values: values})
		}
	}
	return result, nil
}
func (p *powerDNS) Replace(ctx context.Context, zone string, old, next RRSet) error {
	rr := pdnsRRSet{Name: next.Name + ".", Type: next.Type, TTL: next.TTL, ChangeType: "REPLACE"}
	if len(next.Values) == 0 {
		rr.ChangeType = "DELETE"
	}
	for _, value := range next.Values {
		if next.Type == "TXT" {
			value = txtRDATA(value)
		}
		rr.Records = append(rr.Records, pdnsRecord{Content: powerDNSAPIContent(value)})
	}

	return p.request(ctx, http.MethodPatch, zone, map[string]any{"rrsets": []pdnsRRSet{rr}}, nil)
}

// PowerDNS 5.1 normalizes RFC1035 escapes once before parsing RDATA again.
// Preserve the canonical escape for that second pass, without escaping the
// outer quote delimiters. Otherwise literal TXT quotes are rejected and
// backslashes can silently change value.
func powerDNSAPIContent(content string) string {
	var out strings.Builder
	for i := 0; i < len(content); i++ {
		if content[i] == '\\' && i+1 < len(content) {
			out.WriteString(`\\`)
			i++
			if content[i] == '\\' || content[i] == '"' {
				out.WriteByte('\\')
			}
		}
		out.WriteByte(content[i])
	}
	return out.String()
}
