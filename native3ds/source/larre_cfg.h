#pragma once

// Where the LARR-E API is, read from sdmc:/3ds/larre.cfg, or else from the
// copy bundled into the .3dsx at build time (romfs:/larre.cfg):
//
//   url=http://192.168.43.10:8787
//   token=<LARRE_API_TOKEN>
//
// Blank lines and lines starting with # are ignored.

#define LARRE_CFG_PATH "sdmc:/3ds/larre.cfg"
#define LARRE_CFG_BUNDLED "romfs:/larre.cfg"

typedef struct {
	char url[128];   // no trailing slash
	char token[128];
} larre_cfg;

// 0 when both url (http://...) and token are present, else -1.
int cfg_parse(const char *text, larre_cfg *c);
