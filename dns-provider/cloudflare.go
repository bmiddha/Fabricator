package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/libdns/cloudflare"
	"github.com/libdns/libdns"
)

type cloudflareBackend struct{ provider *cloudflare.Provider }

// The pinned libdns adapter enables proxying for cfargotunnel.com CNAMEs.
// Force DNS-only at its HTTP boundary, including this special case.
type dnsOnlyTransport struct{ base http.RoundTripper }
type exactDeleteKey struct{}
type txtWriteKey struct{}

const txtMarker = "!fabricator-libdns-txt!"

type exactDelete struct {
	set   RRSet
	value string
}

func (t dnsOnlyTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if match, ok := req.Context().Value(exactDeleteKey{}).(exactDelete); ok && match.set.Type == "TXT" && req.Method == "GET" && strings.HasSuffix(req.URL.Path, "/dns_records") {
		req = req.Clone(req.Context())
		q := req.URL.Query()
		q.Del("content.contains")
		q.Del("content.exact")
		req.URL.RawQuery = q.Encode()
	}
	if (req.Method == "POST" || req.Method == "PUT" || req.Method == "PATCH") && strings.Contains(req.URL.Path, "/dns_records") && req.Body != nil {
		data, err := io.ReadAll(io.LimitReader(req.Body, 1<<20))
		_ = req.Body.Close()
		if err != nil {
			return nil, err
		}
		var record map[string]any
		if err := json.Unmarshal(data, &record); err != nil {
			return nil, err
		}
		record["proxied"] = false
		if text, ok := req.Context().Value(txtWriteKey{}).(string); ok && record["type"] == "TXT" {
			record["content"] = txtRDATA(text)
		}
		data, err = json.Marshal(record)
		if err != nil {
			return nil, err
		}
		req = req.Clone(req.Context())
		req.Body = io.NopCloser(bytes.NewReader(data))
		req.ContentLength = int64(len(data))
	}
	resp, err := t.base.RoundTrip(req)
	// libdns v0.2.2 looks up SRV by name/type only and MX by target
	// without preference. Filter these lookup results before it deletes IDs.
	if match, ok := req.Context().Value(exactDeleteKey{}).(exactDelete); ok && err == nil &&
		req.Method == "GET" && strings.HasSuffix(req.URL.Path, "/dns_records") && resp.StatusCode == 200 &&
		(match.set.Type == "SRV" || match.set.Type == "MX" || match.set.Type == "TXT") {
		err = filterDeleteResponse(resp, match)
		if err != nil {
			_ = resp.Body.Close()
			return nil, err
		}
	} else if err == nil && req.Method == "GET" && strings.HasSuffix(req.URL.Path, "/dns_records") && resp.StatusCode == 200 {
		err = normalizeCloudflareTXTResponse(resp)
		if err != nil {
			_ = resp.Body.Close()
			return nil, err
		}
	}
	return resp, err
}

// libdns strips TXT delimiters but does not unescape them. Carry raw text
// through its parser under a private sentinel so quotes/backslashes roundtrip.
func normalizeCloudflareTXTResponse(resp *http.Response) error {
	defer resp.Body.Close()
	var envelope map[string]json.RawMessage
	if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&envelope); err != nil {
		return err
	}
	var records []map[string]any
	if err := json.Unmarshal(envelope["result"], &records); err != nil {
		return err
	}
	for _, record := range records {
		if record["type"] != "TXT" {
			continue
		}
		content, ok := record["content"].(string)
		if !ok {
			return fmt.Errorf("Cloudflare TXT content is not a string")
		}
		if strings.HasPrefix(content, `"`) {
			var err error
			content, err = normalizeValue("txt.invalid", "TXT", content, 300)
			if err != nil {
				return err
			}
		}
		record["content"] = txtMarker + content
	}
	envelope["result"], _ = json.Marshal(records)
	data, err := json.Marshal(envelope)
	if err != nil {
		return err
	}
	resp.Body = io.NopCloser(bytes.NewReader(data))
	resp.ContentLength = int64(len(data))
	return nil
}

func filterDeleteResponse(resp *http.Response, match exactDelete) error {
	defer resp.Body.Close()
	var envelope map[string]json.RawMessage
	if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&envelope); err != nil {
		return err
	}
	var records []json.RawMessage
	if err := json.Unmarshal(envelope["result"], &records); err != nil {
		return err
	}
	kept := []json.RawMessage{}
	for _, raw := range records {
		var record struct {
			Name, Type, Content string
			Priority            uint16
			Data                struct {
				Priority, Weight, Port uint16
				Target                 string
			}
		}
		if err := json.Unmarshal(raw, &record); err != nil {
			return err
		}
		if canonicalName(record.Name) != match.set.Name || record.Type != match.set.Type {
			continue
		}
		value := fmt.Sprintf("%d %s", record.Priority, record.Content)
		if record.Type == "SRV" {
			value = fmt.Sprintf("%d %d %d %s", record.Data.Priority, record.Data.Weight, record.Data.Port, record.Data.Target)
		}
		if record.Type == "TXT" {
			value = record.Content
			if !strings.HasPrefix(value, `"`) {
				value = txtRDATA(value)
			}
		}
		normalized, err := normalizeValue(match.set.Name, record.Type, value, match.set.TTL)
		if err != nil {
			return err
		}
		if normalized == match.value {
			if record.Type == "TXT" {
				// Return exactly the representation libdns's private matcher expects.
				var object map[string]any
				if err := json.Unmarshal(raw, &object); err != nil {
					return err
				}
				content := match.value
				if !strings.HasPrefix(content, `"`) && !strings.HasSuffix(content, `"`) {
					content = fmt.Sprintf("%q", content)
				}
				object["content"] = content
				raw, _ = json.Marshal(object)
			}
			kept = append(kept, raw)
		}
	}
	envelope["result"], _ = json.Marshal(kept)
	data, err := json.Marshal(envelope)
	if err != nil {
		return err
	}
	resp.Body = io.NopCloser(bytes.NewReader(data))
	resp.ContentLength = int64(len(data))
	return nil
}
func newCloudflare(token string) *cloudflareBackend {
	return &cloudflareBackend{provider: &cloudflare.Provider{APIToken: token,
		HTTPClient: &http.Client{Timeout: 25 * time.Second, Transport: dnsOnlyTransport{base: http.DefaultTransport},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}}
}

// libdns TXT uses unescaped text; our wire and PowerDNS use zone-file RDATA.
func fromLibDNS(zone string, record libdns.Record) (RRSet, error) {
	rr := record.RR()
	name := canonicalName(libdns.AbsoluteName(rr.Name, zone+"."))
	value := rr.Data
	switch v := record.(type) {
	case libdns.TXT:
		value = txtRDATA(strings.TrimPrefix(v.Text, txtMarker))
	}
	return RRSet{Name: name, Type: rr.Type, TTL: uint32(rr.TTL / time.Second), Values: []string{value}}, nil
}
func toLibDNS(zone string, set RRSet, value string) (libdns.Record, error) {
	rr := libdns.RR{Name: libdns.RelativeName(set.Name+".", zone+"."), Type: set.Type,
		TTL: time.Duration(set.TTL) * time.Second, Data: value}
	if rr.Name == "" {
		rr.Name = "@"
	}
	if set.Type == "TXT" {
		return libdns.TXT{Name: rr.Name, TTL: rr.TTL, Text: value}, nil
	}
	return rr.Parse()
}
func (p *cloudflareBackend) List(ctx context.Context, zone string) ([]RRSet, error) {
	records, err := p.provider.GetRecords(ctx, zone+".")
	if err != nil {
		return nil, err
	}
	grouped := map[string]RRSet{}
	for _, record := range records {
		rr, err := fromLibDNS(zone, record)
		if err != nil {
			return nil, err
		}
		if !supported[rr.Type] {
			continue
		}
		previous := grouped[key(rr)]
		if len(previous.Values) > 0 && previous.TTL != rr.TTL {
			return nil, fmt.Errorf("Cloudflare RRset has inconsistent TTL: %s", key(rr))
		}
		rr.Values = append(previous.Values, rr.Values...)
		grouped[key(rr)] = rr
	}
	result := []RRSet{}
	for _, rr := range grouped {
		result = append(result, rr)
	}
	return result, nil
}
func (p *cloudflareBackend) Replace(ctx context.Context, zone string, old, next RRSet) error {
	// Convert the whole operation before touching the provider.
	before, after := map[string]libdns.Record{}, map[string]libdns.Record{}
	for _, value := range old.Values {
		r, err := toLibDNS(zone, old, value)
		if err != nil {
			return err
		}
		before[value] = r
	}
	for _, value := range next.Values {
		r, err := toLibDNS(zone, next, value)
		if err != nil {
			return err
		}
		after[value] = r
	}
	// Never use libdns.SetRecords: its loose lookup can overwrite a sibling value.
	add, remove := []string{}, []string{}
	for v := range after {
		if _, ok := before[v]; !ok {
			add = append(add, v)
		}
	}
	for v := range before {
		if _, ok := after[v]; !ok || old.TTL != next.TTL {
			remove = append(remove, v)
		}
	}
	sort.Strings(add)
	sort.Strings(remove)
	// Cloudflare forbids two CNAME values at one name. A target change must
	// remove the old singleton before adding its replacement.
	if old.Type == "CNAME" && len(old.Values) > 0 && len(add) > 0 {
		deleteCtx := context.WithValue(ctx, exactDeleteKey{}, exactDelete{set: old, value: old.Values[0]})
		if _, err := p.provider.DeleteRecords(deleteCtx, zone+".", []libdns.Record{before[old.Values[0]]}); err != nil {
			return err
		}
		remove = nil
	}
	for _, v := range add {
		writeCtx := context.WithValue(ctx, txtWriteKey{}, v)
		if _, err := p.provider.AppendRecords(writeCtx, zone+".", []libdns.Record{after[v]}); err != nil {
			return err
		}
	}
	for _, v := range remove {
		deleteCtx := context.WithValue(ctx, exactDeleteKey{}, exactDelete{set: old, value: v})
		if _, err := p.provider.DeleteRecords(deleteCtx, zone+".", []libdns.Record{before[v]}); err != nil {
			return err
		}
		if replacement, ok := after[v]; ok {
			writeCtx := context.WithValue(ctx, txtWriteKey{}, v)
			if _, err := p.provider.AppendRecords(writeCtx, zone+".", []libdns.Record{replacement}); err != nil {
				return err
			}
		}
	}
	return nil
}
