#include "larre_cfg.h"

#include <ctype.h>
#include <string.h>

// Copy [start, end) into dst with surrounding whitespace removed.
static void copy_trimmed(char *dst, size_t size, const char *start, const char *end)
{
	while (start < end && isspace((unsigned char)*start))
		start++;
	while (end > start && isspace((unsigned char)end[-1]))
		end--;
	size_t n = (size_t)(end - start);
	if (n >= size)
		n = size - 1;
	memcpy(dst, start, n);
	dst[n] = '\0';
}

int cfg_parse(const char *text, larre_cfg *c)
{
	memset(c, 0, sizeof(*c));
	for (const char *line = text; *line; ) {
		const char *end = strchr(line, '\n');
		if (!end)
			end = line + strlen(line);
		const char *eq = memchr(line, '=', (size_t)(end - line));
		if (eq && line[0] != '#') {
			char key[16];
			copy_trimmed(key, sizeof(key), line, eq);
			if (strcmp(key, "url") == 0)
				copy_trimmed(c->url, sizeof(c->url), eq + 1, end);
			else if (strcmp(key, "token") == 0)
				copy_trimmed(c->token, sizeof(c->token), eq + 1, end);
		}
		line = *end ? end + 1 : end;
	}
	size_t n = strlen(c->url);
	while (n > 0 && c->url[n - 1] == '/')
		c->url[--n] = '\0';
	if (strncmp(c->url, "http://", 7) != 0) // plain HTTP on the LAN; no TLS certificates to manage
		return -1;
	return c->token[0] ? 0 : -1;
}
