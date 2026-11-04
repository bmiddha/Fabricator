package main

import (
	"net/http/httptest"
	"strings"
	"testing"

	"sigs.k8s.io/external-dns/endpoint"
)

func TestCanonicalProviderAndWireValues(t *testing.T) {
	for _, tc := range []struct{ typ, input, want string }{
		{"CNAME", "MC.EXAMPLE.ORG", "mc.example.org."},
		{"NS", "NS1.EXAMPLE.ORG", "ns1.example.org."},
		{"MX", "10 MAIL.EXAMPLE.ORG", "10 mail.example.org."},
		{"SRV", "0 0 25565 MC.EXAMPLE.ORG", "0 0 25565 mc.example.org."},
		{"AAAA", "2001:0db8:0:0::1", "2001:db8::1"},
		{"TXT", `"a" "b"`, "ab"},
		{"TXT", `"quotes \" and slash \\"`, `quotes " and slash \`},
	} {
		got, err := normalizeValue("mc.games.example.org", tc.typ, tc.input, 300)
		if err != nil || got != tc.want {
			t.Errorf("%s got=%q want=%q err=%v", tc.typ, got, tc.want, err)
		}
	}
	for _, text := range []string{"", `"literal quotes"`, `one\slash`, "snowman ☃", strings.Repeat("x", 256), strings.Repeat("☃", 100)} {
		got, err := normalizeValue("txt.games.example.org", "TXT", txtRDATA(text), 300)
		if err != nil || got != text {
			t.Errorf("TXT roundtrip got=%q want=%q err=%v", got, text, err)
		}
	}
}

func TestEmptyScopeStillNegotiatesAndReturnsArray(t *testing.T) {
	s, _ := testService()
	for _, path := range []string{"/", "/records"} {
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 200 || w.Header().Get("Content-Type") != mediaType {
			t.Fatalf("%s %d", path, w.Code)
		}

		if path == "/records" && strings.TrimSpace(w.Body.String()) != "[]" {
			t.Fatal(w.Body)
		}
		if path == "/" && !strings.Contains(w.Body.String(), `"include":["games.example.org"]`) {
			t.Fatal(w.Body)
		}
	}
	w := post(t, s, "/adjustendpoints", []*endpoint.Endpoint{ep("_owner.games.example.org", "TXT", `heritage=external-dns,owner="fabricator"`)})
	if w.Code != 200 || !strings.Contains(w.Body.String(), `heritage=external-dns,owner=\"fabricator\"`) {
		t.Fatalf("%d: %s", w.Code, w.Body)
	}
}

func TestPowerDNSAPIEscapeProtection(t *testing.T) {
	for _, tc := range []struct{ input, want string }{
		{`"plain"`, `"plain"`},
		{`"owner=\"fabricator\""`, `"owner=\\\"fabricator\\\""`},
		{`"slash \\"`, `"slash \\\\"`},
		{`"snowman \226\152\131"`, `"snowman \\226\\152\\131"`},
	} {
		if got := powerDNSAPIContent(tc.input); got != tc.want {
			t.Errorf("got=%q want=%q", got, tc.want)
		}
	}
}
