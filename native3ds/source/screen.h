#pragma once

#include <stdbool.h>

// The bottom-screen text, parsed from GET /api/screen (api_server.screen_lines).
// Each line there is "<tag> <text>":
//   U unit row, N note, C critical alert title, A other alert title, a alert text,
//   P "<approval id> <title>" of a pending hardware action, p its arguments.

#define SCREEN_MAX_LINES 20
#define SCREEN_COLS 40     // the bottom-screen console is 40x30 characters
#define APPROVAL_ID_MAX 32

typedef struct {
	char tag[SCREEN_MAX_LINES];
	char text[SCREEN_MAX_LINES][SCREEN_COLS + 1];
	int n;
	char approval_id[APPROVAL_ID_MAX + 1]; // "" when nothing is waiting
	bool critical;
} larre_screen;

// Parse a response body. Returns the number of lines kept.
int screen_parse(const char *body, larre_screen *s);
