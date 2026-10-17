#include "screen.h"

#include <ctype.h>
#include <string.h>

static void copy_line(char *dst, size_t size, const char *start, const char *end)
{
	if (end > start && end[-1] == '\r')
		end--;
	size_t n = (size_t)(end - start);
	if (n >= size)
		n = size - 1;
	memcpy(dst, start, n);
	dst[n] = '\0';
}

// The id ends up in a URL path, so accept only what the API issues: lowercase hex.
static bool take_approval_id(char *dst, const char *text, const char **rest)
{
	size_t n = 0;
	while (n < APPROVAL_ID_MAX && isxdigit((unsigned char)text[n]) && !isupper((unsigned char)text[n]))
		n++;
	if (n == 0 || (text[n] != ' ' && text[n] != '\0'))
		return false;
	memcpy(dst, text, n);
	dst[n] = '\0';
	*rest = text[n] ? text + n + 1 : text + n;
	return true;
}

int screen_parse(const char *body, larre_screen *s)
{
	memset(s, 0, sizeof(*s));
	for (const char *line = body; *line && s->n < SCREEN_MAX_LINES; ) {
		const char *end = strchr(line, '\n');
		if (!end)
			end = line + strlen(line);
		if (end - line >= 2 && line[1] == ' ' && strchr("UNCAaPp", line[0])) {
			char tag = line[0];
			char text[256];
			copy_line(text, sizeof(text), line + 2, end);
			const char *shown = text;
			if (tag == 'P' && !take_approval_id(s->approval_id, text, &shown))
				shown = text; // keep the title, but there is nothing to approve
			if (tag == 'C')
				s->critical = true;
			s->tag[s->n] = tag;
			copy_line(s->text[s->n], sizeof(s->text[s->n]), shown, shown + strlen(shown));
			s->n++;
		}
		line = *end ? end + 1 : end;
	}
	return s->n;
}
