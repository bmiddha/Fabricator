package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/miekg/dns"
	"sigs.k8s.io/external-dns/endpoint"
	"sigs.k8s.io/external-dns/plan"
)

const mediaType = "application/external.dns.webhook+json;version=1"

var supported = map[string]bool{"A": true, "AAAA": true, "CNAME": true, "SRV": true, "TXT": true, "MX": true, "CAA": true, "NS": true}

type RRSet struct {
	Name   string   `json:"name"`
	Type   string   `json:"type"`
	TTL    uint32   `json:"ttl"`
	Values []string `json:"values"`
}

type Backend interface {
	List(context.Context, string) ([]RRSet, error)
	Replace(context.Context, string, RRSet, RRSet) error
}

type Service struct {
	backend        Backend
	zones, domains []string
	mu             sync.Mutex
}

func canonicalName(s string) string   { return strings.ToLower(strings.TrimSuffix(s, ".")) }
func within(name, domain string) bool { return name == domain || strings.HasSuffix(name, "."+domain) }
func zoneFor(name string, zones []string) string {
	best := ""
	for _, zone := range zones {
		if within(name, zone) && len(zone) > len(best) {
			best = zone
		}
	}
	return best
}
func validName(name string, wildcard bool) bool {
	if name == "" || len(name) > 253 {
		return false
	}
	for i, label := range strings.Split(name, ".") {
		if wildcard && i == 0 && label == "*" {
			continue
		}
		if label == "" || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, c := range label {
			if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
				return false
			}
		}
	}
	return true
}
func key(r RRSet) string { return r.Name + "/" + r.Type }
func same(a, b RRSet) bool {
	return (len(a.Values) == 0 && len(b.Values) == 0) || a.TTL == b.TTL && strings.Join(a.Values, "\x00") == strings.Join(b.Values, "\x00")
}
func normalizeValue(name, typ, value string, ttl uint32) (string, error) {
	if strings.ContainsAny(value, "\r\n\x00") {
		return "", errors.New("control characters in RDATA")
	}
	rr, err := dns.NewRR(fmt.Sprintf("%s. %d IN %s %s", name, ttl, typ, value))
	if err != nil {
		return "", fmt.Errorf("invalid %s RDATA: %w", typ, err)
	}
	if rr.Header().Rrtype != dns.StringToType[typ] {
		return "", errors.New("RDATA type mismatch")
	}
	switch r := rr.(type) {
	case *dns.TXT:
		var raw strings.Builder
		for _, chunk := range r.Txt {
			for i := 0; i < len(chunk); i++ {
				if chunk[i] == '\\' && i+1 < len(chunk) {
					i++
					if i+2 < len(chunk) && chunk[i] >= '0' && chunk[i] <= '9' &&
						chunk[i+1] >= '0' && chunk[i+1] <= '9' && chunk[i+2] >= '0' && chunk[i+2] <= '9' {
						n, _ := strconv.Atoi(chunk[i : i+3])
						raw.WriteByte(byte(n))
						i += 2
						continue
					}
				}
				raw.WriteByte(chunk[i])
			}
		}
		return raw.String(), nil
	case *dns.CNAME:
		r.Target = dns.Fqdn(strings.ToLower(r.Target))
	case *dns.NS:
		r.Ns = dns.Fqdn(strings.ToLower(r.Ns))
	case *dns.MX:
		r.Mx = dns.Fqdn(strings.ToLower(r.Mx))
	case *dns.SRV:
		r.Target = dns.Fqdn(strings.ToLower(r.Target))
	}
	text := rr.String()
	return strings.TrimPrefix(text, rr.Header().String()), nil
}
func txtRDATA(text string) string {
	chunks := []string{}
	for len(text) > 255 {
		chunks = append(chunks, text[:255])
		text = text[255:]
	}
	chunks = append(chunks, text)
	encoded := make([]string, len(chunks))
	for i, chunk := range chunks {
		var out strings.Builder
		out.WriteByte('"')
		for _, b := range []byte(chunk) {
			switch {
			case b == '\\' || b == '"':
				out.WriteByte('\\')
				out.WriteByte(b)
			case b < 32 || b > 126:
				fmt.Fprintf(&out, `\%03d`, b)
			default:
				out.WriteByte(b)
			}
		}
		out.WriteByte('"')
		encoded[i] = out.String()
	}
	return strings.Join(encoded, " ")
}
func (s *Service) normalize(ep *endpoint.Endpoint) (RRSet, error) {
	if ep == nil {
		return RRSet{}, errors.New("null endpoint")
	}
	r := RRSet{Name: canonicalName(ep.DNSName), Type: strings.ToUpper(ep.RecordType)}
	if !validName(r.Name, true) || zoneFor(r.Name, s.zones) == "" || zoneFor(r.Name, s.domains) == "" {
		return r, errors.New("name outside configured DNS scope or invalid")
	}
	if !supported[r.Type] {
		return r, errors.New("unsupported record type")
	}
	if ep.SetIdentifier != "" || len(ep.ProviderSpecific) != 0 {
		return r, errors.New("routing and provider-specific options are not supported")
	}
	if ep.RecordTTL < 0 || uint64(ep.RecordTTL) > math.MaxUint32 {
		return r, errors.New("TTL outside uint32 range")
	}
	r.TTL = uint32(ep.RecordTTL)
	if r.TTL == 0 {
		r.TTL = 300
	}
	if len(ep.Targets) == 0 {
		return r, errors.New("targets required")
	}
	seen := map[string]bool{}
	for _, v := range ep.Targets {
		if r.Type == "TXT" {
			if strings.ContainsAny(v, "\r\n\x00") || len(v) > 65000 {
				return r, errors.New("TXT target contains control characters or exceeds 65000 bytes")
			}
			v = txtRDATA(v)
		}
		n, err := normalizeValue(r.Name, r.Type, v, r.TTL)
		if err != nil {
			return r, err
		}
		if seen[n] {
			return r, errors.New("duplicate target")
		}
		seen[n] = true
		r.Values = append(r.Values, n)
	}
	sort.Strings(r.Values)
	if r.Type == "CNAME" && (len(r.Values) != 1 || r.Name == zoneFor(r.Name, s.zones)) {
		return r, errors.New("CNAME must have one target and cannot be a zone apex")
	}
	return r, nil
}

func (s *Service) Handler() http.Handler {
	m := http.NewServeMux()
	m.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		if a := r.Header.Get("Accept"); a != "" && a != "*/*" && a != mediaType {
			http.Error(w, "unsupported webhook version", 406)
			return
		}
		writeJSON(w, 200, endpoint.NewDomainFilter(s.domains))
	})
	m.HandleFunc("GET /records", s.records)
	m.HandleFunc("POST /adjustendpoints", func(w http.ResponseWriter, r *http.Request) {
		var eps []*endpoint.Endpoint
		if !decode(w, r, &eps) {
			return
		}
		for _, ep := range eps {
			v, err := s.normalize(ep)
			if err != nil {
				writeJSON(w, 400, map[string]string{"error": err.Error()})
				return
			}
			ep.DNSName, ep.RecordType, ep.RecordTTL, ep.Targets = v.Name, v.Type, endpoint.TTL(v.TTL), v.Values
		}
		if eps == nil {
			eps = []*endpoint.Endpoint{}
		}
		writeJSON(w, 200, eps)
	})
	m.HandleFunc("POST /records", s.apply)
	return m
}
func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	if ct := r.Header.Get("Content-Type"); ct != mediaType && ct != "application/json" {
		writeJSON(w, 415, map[string]string{"error": "unsupported content type"})
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	err := dec.Decode(dst)
	if err == nil {
		var extra any
		if dec.Decode(&extra) != io.EOF {
			err = errors.New("expected a single JSON document")
		}
	}
	if err != nil {
		status := 400
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			status = 413
		}
		writeJSON(w, status, map[string]string{"error": err.Error()})
		return false
	}
	return true
}
func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", mediaType)
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
func (s *Service) snapshot(ctx context.Context) (map[string]RRSet, error) {
	result := map[string]RRSet{}
	for _, zone := range s.zones {
		records, err := s.backend.List(ctx, zone)
		if err != nil {
			return nil, err
		}
		for _, rr := range records {
			rr.Name = canonicalName(rr.Name)
			if !supported[rr.Type] || zoneFor(rr.Name, s.zones) != zone || zoneFor(rr.Name, s.domains) == "" {
				continue
			}
			for i, v := range rr.Values {
				rr.Values[i], err = normalizeValue(rr.Name, rr.Type, v, rr.TTL)
				if err != nil {
					return nil, err
				}
			}
			sort.Strings(rr.Values)
			result[key(rr)] = rr
		}
	}
	return result, nil
}
func (s *Service) records(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	rows, err := s.snapshot(ctx)
	if err != nil {
		slog.Error("DNS listing failed", "error", err)
		writeJSON(w, 502, map[string]string{"error": "provider listing failed"})
		return
	}
	eps := []*endpoint.Endpoint{}
	keys := make([]string, 0, len(rows))
	for k := range rows {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		v := rows[k]
		eps = append(eps, &endpoint.Endpoint{DNSName: v.Name, RecordType: v.Type, RecordTTL: endpoint.TTL(v.TTL), Targets: endpoint.Targets(v.Values)})
	}
	writeJSON(w, 200, eps)
}

type operation struct{ old, next RRSet }

func (s *Service) operations(c plan.Changes) ([]operation, error) {
	if len(c.UpdateOld) != len(c.UpdateNew) {
		return nil, errors.New("updateOld/updateNew must be paired")
	}
	ops := []operation{}
	seen := map[string]bool{}
	add := func(oldEP, newEP *endpoint.Endpoint) error {
		var o operation
		var err error
		if oldEP != nil {
			o.old, err = s.normalize(oldEP)
			if err != nil {
				return err
			}
		}
		if newEP != nil {
			o.next, err = s.normalize(newEP)
			if err != nil {
				return err
			}
		}
		if oldEP == nil {
			o.old = RRSet{Name: o.next.Name, Type: o.next.Type}
		}
		if newEP == nil {
			o.next = RRSet{Name: o.old.Name, Type: o.old.Type}
		}
		if key(o.old) != key(o.next) {
			return errors.New("update cannot rename or change record type; use delete/create")
		}
		if seen[key(o.old)] {
			return errors.New("multiple operations on one RRset")
		}
		seen[key(o.old)] = true
		ops = append(ops, o)
		return nil
	}
	for _, ep := range c.Create {
		if ep == nil {
			return nil, errors.New("null endpoint")
		}
		if err := add(nil, ep); err != nil {
			return nil, err
		}
	}
	for i, ep := range c.UpdateOld {
		if ep == nil || c.UpdateNew[i] == nil {
			return nil, errors.New("null update endpoint")
		}
		if err := add(ep, c.UpdateNew[i]); err != nil {
			return nil, err
		}
	}
	for _, ep := range c.Delete {
		if ep == nil {
			return nil, errors.New("null endpoint")
		}
		if err := add(ep, nil); err != nil {
			return nil, err
		}
	}
	return ops, nil
}
func (s *Service) apply(w http.ResponseWriter, r *http.Request) {
	var changes plan.Changes
	if !decode(w, r, &changes) {
		return
	}
	ops, err := s.operations(changes)
	if err != nil {
		writeJSON(w, 400, map[string]string{"error": err.Error()})
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	current, err := s.snapshot(ctx)
	if err != nil {
		slog.Error("DNS preflight failed", "error", err)
		writeJSON(w, 502, map[string]string{"error": "provider listing failed; no writes attempted"})
		return
	}
	for _, op := range ops {
		live := current[key(op.old)]
		if !same(live, op.old) {
			writeJSON(w, 409, map[string]string{"error": "previous RRset differs; refresh before retrying", "rrset": key(op.old)})
			return
		}
	}
	final := make(map[string]RRSet, len(current))
	for k, v := range current {
		final[k] = v
	}
	for _, op := range ops {
		final[key(op.next)] = op.next
	}
	for _, v := range final {
		if len(v.Values) == 0 {
			continue
		}
		if c := final[v.Name+"/CNAME"]; v.Type != "CNAME" && len(c.Values) > 0 {
			writeJSON(w, 400, map[string]string{"error": "CNAME cannot coexist with other record types"})
			return
		}
	}
	// Delete first, allowing a CNAME/type transition without deleting unrelated RRsets.
	sort.SliceStable(ops, func(i, j int) bool { return len(ops[i].next.Values) == 0 && len(ops[j].next.Values) != 0 })
	applied := []string{}
	for _, op := range ops {
		if same(op.old, op.next) {
			continue
		}
		// Re-read before each write to catch external edits observed since preflight.
		latest, err := s.snapshot(ctx)
		if err == nil && !same(latest[key(op.old)], op.old) {
			err = errors.New("RRset changed during request")
		}
		if err == nil {
			err = s.backend.Replace(ctx, zoneFor(op.old.Name, s.zones), op.old, op.next)
		}
		if err == nil {
			var verified map[string]RRSet
			verified, err = s.snapshot(ctx)
			if err == nil && !same(verified[key(op.next)], op.next) {
				err = errors.New("provider state did not match requested RRset after write")
			}
		}
		if err != nil {
			slog.Error("DNS mutation failed", "rrset", key(op.old), "error", err)
			writeJSON(w, 502, map[string]any{"error": "mutation failed; failed RRset may be partially changed; refresh before retrying", "failed": key(op.old), "applied": applied})
			return
		}
		applied = append(applied, key(op.old))
	}
	w.WriteHeader(http.StatusNoContent)
}
