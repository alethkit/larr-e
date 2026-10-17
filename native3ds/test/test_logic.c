// Host-side tests for the parts of the app that don't touch 3DS hardware.
// Run by tests/test_native3ds_logic.py (or: cc -I source source/larre_cfg.c
// source/screen.c test/test_logic.c && ./a.out).

#include <stdio.h>
#include <string.h>

#include "larre_cfg.h"
#include "screen.h"

static int failures = 0;

#define EXPECT(cond) do { \
	if (!(cond)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } \
} while (0)

static void test_cfg(void)
{
	larre_cfg c;
	const char *text =
		"# LARR-E API on the laptop\r\n"
		"url = http://192.168.43.10:8787/\r\n"
		"token=abc123\n";
	EXPECT(cfg_parse(text, &c) == 0);
	EXPECT(strcmp(c.url, "http://192.168.43.10:8787") == 0); // trailing slash dropped
	EXPECT(strcmp(c.token, "abc123") == 0);

	EXPECT(cfg_parse("url=http://x:1\n", &c) == -1);   // no token
	EXPECT(cfg_parse("token=t\n", &c) == -1);          // no url
	EXPECT(cfg_parse("url=ftp://x\ntoken=t\n", &c) == -1);  // only plain http
	EXPECT(cfg_parse("url=https://x\ntoken=t\n", &c) == -1);
}

static void test_screen(void)
{
	larre_screen s;
	const char *body =
		"U pio01  OD 0.123 37.0C 0.12/h  499rpm\n"
		"C CRITICAL pio01 stirring\n"
		"a Stirring RPM dropped to 0\n"
		"P 0a1b2c Change temperature\n"
		"p celsius = 30.0\n";
	EXPECT(screen_parse(body, &s) == 5);
	EXPECT(s.tag[0] == 'U' && strcmp(s.text[0], "pio01  OD 0.123 37.0C 0.12/h  499rpm") == 0);
	EXPECT(s.critical);
	EXPECT(strcmp(s.approval_id, "0a1b2c") == 0);
	EXPECT(strcmp(s.text[3], "Change temperature") == 0); // id stripped from the title
	EXPECT(s.tag[4] == 'p' && strcmp(s.text[4], "celsius = 30.0") == 0);

	// No approval, no critical alert, and junk lines are skipped.
	EXPECT(screen_parse("N No telemetry yet\n\nbad\nA WARNING x y\n", &s) == 2);
	EXPECT(s.approval_id[0] == '\0');
	EXPECT(!s.critical);

	// A non-hex approval id is not trusted (it goes into a URL path).
	EXPECT(screen_parse("P ../x Run a pump\n", &s) == 1);
	EXPECT(s.approval_id[0] == '\0');

	// Long lines are cut to the console width, and the line count is capped.
	char big[4096] = "";
	for (int i = 0; i < SCREEN_MAX_LINES + 5; i++)
		strcat(big, "U 0123456789012345678901234567890123456789EXTRA\n");
	EXPECT(screen_parse(big, &s) == SCREEN_MAX_LINES);
	EXPECT(strlen(s.text[0]) == SCREEN_COLS);
}

int main(void)
{
	test_cfg();
	test_screen();
	if (failures) {
		printf("%d failure(s)\n", failures);
		return 1;
	}
	printf("ok\n");
	return 0;
}
