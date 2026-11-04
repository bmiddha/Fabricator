package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"sigs.k8s.io/external-dns/endpoint"
	"sigs.k8s.io/external-dns/plan"
)

func TestPowerDNSREST(t *testing.T) {
	var patched struct {
		RRSets []pdnsRRSet `json:"rrsets"`
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/servers/localhost/zones/example.org." || r.Header.Get("X-API-Key") != "private-key" {
			t.Errorf("wrong request %s", r.URL.Path)
		}
		if r.Method == "GET" {
			_, _ = io.WriteString(w, `{"rrsets":[{"name":"mc.games.example.org.","type":"A","ttl":300,"records":[{"content":"192.0.2.1","disabled":false}]},{"name":"example.org.","type":"SOA","ttl":300,"records":[]} ]}`)
		} else if r.Method == "PATCH" {
			if err := json.NewDecoder(r.Body).Decode(&patched); err != nil {
				t.Error(err)
			}
			w.WriteHeader(204)
		} else {
			t.Error(r.Method)
		}
	}))
	defer server.Close()
	p := newPowerDNS(server.URL, "private-key", "localhost")
	rrs, err := p.List(context.Background(), "example.org")
	if err != nil || len(rrs) != 1 || rrs[0].Values[0] != "192.0.2.1" {
		t.Fatalf("%+v %v", rrs, err)
	}
	next := RRSet{Name: "mc.games.example.org", Type: "A", TTL: 600, Values: []string{"192.0.2.2", "192.0.2.3"}}
	if err := p.Replace(context.Background(), "example.org", rrs[0], next); err != nil {
		t.Fatal(err)
	}
	if len(patched.RRSets) != 1 || len(patched.RRSets[0].Records) != 2 || patched.RRSets[0].ChangeType != "REPLACE" || patched.RRSets[0].TTL != 600 {
		t.Fatalf("%+v", patched)
	}
	next.Values = nil
	if err := p.Replace(context.Background(), "example.org", rrs[0], next); err != nil {
		t.Fatal(err)
	}
	if patched.RRSets[0].ChangeType != "DELETE" {
		t.Fatal(patched)
	}
}
func TestPowerDNSPrivateURL(t *testing.T) {
	for _, raw := range []string{"http://127.0.0.1:8081", "http://powerdns:8081", "http://192.168.1.3:8081", "https://dns.example.org"} {
		if err := validatePowerDNSURL(raw); err != nil {
			t.Errorf("%s: %v", raw, err)
		}
	}
	for _, raw := range []string{"", "http://public.example.org", "http://8.8.8.8", "https://key@dns.example.org", "https://dns.example.org?key=secret"} {
		if validatePowerDNSURL(raw) == nil {
			t.Errorf("accepted %s", raw)
		}
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func TestCloudflareDNSOnly(t *testing.T) {
	transport := dnsOnlyTransport{base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		var data map[string]any
		if err := json.NewDecoder(r.Body).Decode(&data); err != nil {
			t.Fatal(err)
		}
		if data["proxied"] != false {
			t.Fatal("Cloudflare proxy enabled")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("{}"))}, nil
	})}
	req := httptest.NewRequest("POST", "https://api.cloudflare.com/client/v4/zones/z/dns_records", strings.NewReader(`{"type":"CNAME","content":"id.cfargotunnel.com","proxied":true}`))
	resp, err := transport.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
}
func TestCloudflareExactDeleteFiltering(t *testing.T) {
	for _, tc := range []struct{ typ, value, body string }{
		{"SRV", "0 5 25565 mc.example.org.", `{"success":true,"result":[{"id":"wanted","type":"SRV","name":"_minecraft._tcp.games.example.org","data":{"priority":0,"weight":5,"port":25565,"target":"mc.example.org."}},{"id":"keep","type":"SRV","name":"_minecraft._tcp.games.example.org","data":{"priority":0,"weight":5,"port":25566,"target":"mc.example.org."}}]}`},
		{"MX", "10 mail.example.org.", `{"success":true,"result":[{"id":"wanted","type":"MX","name":"_minecraft._tcp.games.example.org","content":"mail.example.org","priority":10},{"id":"keep","type":"MX","name":"_minecraft._tcp.games.example.org","content":"mail.example.org","priority":20}]}`},
	} {
		resp := &http.Response{Body: io.NopCloser(strings.NewReader(tc.body))}
		err := filterDeleteResponse(resp, exactDelete{set: RRSet{Name: "_minecraft._tcp.games.example.org", Type: tc.typ, TTL: 300}, value: tc.value})
		if err != nil {
			t.Fatal(err)
		}

		data, _ := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if strings.Contains(string(data), `"keep"`) || !strings.Contains(string(data), `"wanted"`) {
			t.Fatal(string(data))
		}
	}
}

func TestPinnedCloudflareAdapterRoundTrip(t *testing.T) {
	stored := map[string]map[string]any{}
	sequence := 0
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer scoped-test-token" {
			t.Error("missing provider credential")
		}
		respond := func(result any) {
			_ = json.NewEncoder(w).Encode(map[string]any{"success": true, "result": result,
				"result_info": map[string]int{"total_count": len(stored), "per_page": 100, "page": 1}})
		}
		if r.URL.Path == "/client/v4/zones" {
			respond([]map[string]any{{"id": "zone-id", "name": "example.org"}})
			return
		}
		if r.URL.Path == "/client/v4/zones/zone-id/dns_records" {
			switch r.Method {
			case "POST":
				var record map[string]any
				if err := json.NewDecoder(r.Body).Decode(&record); err != nil {
					t.Error(err)
				}
				if record["proxied"] != false {
					t.Error("record was not DNS-only")
				}
				sequence++
				id := fmt.Sprint(sequence)
				record["id"] = id
				if !within(canonicalName(record["name"].(string)), "example.org") {
					record["name"] = record["name"].(string) + ".example.org"
				}
				if record["type"] == "CNAME" {
					for _, existing := range stored {
						if existing["name"] == record["name"] && existing["type"] == "CNAME" {
							w.WriteHeader(422)
							return
						}
					}
				}
				stored[id] = record
				respond(record)
				return
			case "GET":
				result := []map[string]any{}
				for _, record := range stored {
					q := r.URL.Query()
					if q.Get("type") != "" && q.Get("type") != record["type"] {
						continue
					}
					if q.Get("name") != "" && canonicalName(q.Get("name")) != canonicalName(record["name"].(string)) {
						continue
					}
					if q.Get("content.exact") != "" && strings.TrimSuffix(q.Get("content.exact"), ".") != strings.TrimSuffix(record["content"].(string), ".") {
						continue
					}
					if q.Get("content.contains") != "" && !strings.Contains(record["content"].(string), q.Get("content.contains")) {
						continue
					}
					result = append(result, record)
				}
				respond(result)
				return
			}
		}
		if r.Method == "DELETE" {
			id := strings.TrimPrefix(r.URL.Path, "/client/v4/zones/zone-id/dns_records/")
			delete(stored, id)
			respond(map[string]string{"id": id})
			return
		}
		t.Errorf("unexpected Cloudflare request %s %s", r.Method, r.URL)
		w.WriteHeader(500)
	}))
	defer api.Close()
	p := newCloudflare("scoped-test-token")
	p.provider.HTTPClient = &http.Client{Transport: dnsOnlyTransport{base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		copy := r.Clone(r.Context())
		copy.URL.Scheme = "http"
		copy.URL.Host = strings.TrimPrefix(api.URL, "http://")
		return http.DefaultTransport.RoundTrip(copy)
	})}}
	s := &Service{backend: p, zones: []string{"example.org"}, domains: []string{"games.example.org"}}
	records := []*endpoint.Endpoint{
		ep("a.games.example.org", "A", "192.0.2.1", "192.0.2.2"),
		ep("aaaa.games.example.org", "AAAA", "2001:db8::1"),
		ep("alias.games.example.org", "CNAME", "id.cfargotunnel.com."),
		ep("_minecraft._tcp.games.example.org", "SRV", "0 0 25565 a.games.example.org.", "0 0 25566 a.games.example.org."),
		ep("mail.games.example.org", "MX", "10 mail.example.org.", "20 mail.example.org."),
		ep("txt.games.example.org", "TXT", "heritage=external-dns,owner=fabricator", `quotes " and slash \`, `"starts and ends quoted"`, "snowman ☃"),
		ep("caa.games.example.org", "CAA", `0 issue "letsencrypt.org"`),
		ep("delegated.games.example.org", "NS", "ns.example.org."),
	}
	w := post(t, s, "/records", plan.Changes{Create: records})
	if w.Code != 204 {
		t.Fatalf("%d: %s", w.Code, w.Body)
	}
	// Removing one SRV/MX value must not remove its same-target sibling.
	for _, index := range []int{2, 3, 4} {
		old := records[index]
		var next *endpoint.Endpoint
		if old.RecordType == "CNAME" {
			next = ep(old.DNSName, old.RecordType, "replacement.example.org.")
		} else {
			next = ep(old.DNSName, old.RecordType, old.Targets[1])
		}
		w = post(t, s, "/records", plan.Changes{UpdateOld: []*endpoint.Endpoint{old}, UpdateNew: []*endpoint.Endpoint{next}})
		if w.Code != 204 {
			t.Fatalf("%s: %d %s", old.RecordType, w.Code, w.Body)
		}
		records[index] = next
	}
	w = post(t, s, "/records", plan.Changes{Delete: records})
	if w.Code != 204 || len(stored) != 0 {
		t.Fatalf("%d: %s remaining=%+v", w.Code, w.Body, stored)
	}
}
