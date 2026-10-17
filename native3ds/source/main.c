// LARR-E Cam: live outer-camera feed on the top screen. The bottom screen
// shows the camera reading and, when sdmc:/3ds/larre.cfg points at a LARR-E
// API, the units, the latest alert and any hardware action waiting for
// approval (A approves, B denies). It talks to the API directly over HTTP; no
// bridge on the computer.
//
// Camera setup follows devkitPro's 3ds-examples camera/video example, cut
// down to one camera (outer right, OUT1) at QVGA (320x240).

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <3ds.h>

#include "classify.h"
#include "larre_cfg.h"
#include "screen.h"

#define CAM_W 320
#define CAM_H 240
#define CAM_BYTES (CAM_W * CAM_H * 2) // RGB565
#define TOP_W 400
#define TOP_H 240
#define X_OFF ((TOP_W - CAM_W) / 2)   // centre the image horizontally
#define WAIT_TIMEOUT 1000000000ULL    // 1 s, in ns

static u16 cam_buf[CAM_W * CAM_H] __attribute__((aligned(0x80)));
static u8 rgb_buf[CAM_W * CAM_H * 3];

// Print the call and its result only when it fails, so the bottom screen
// stays readable.
#define CHECK(call) do { \
	Result _r = (call); \
	if (R_FAILED(_r)) printf("%s: 0x%08lX\n", #call, (unsigned long)_r); \
} while (0)

static void rgb565_to_rgb888(const u16 *src, u8 *dst, int n)
{
	for (int i = 0; i < n; i++) {
		u16 v = src[i];
		dst[3 * i + 0] = ((v >> 11) & 0x1F) << 3;
		dst[3 * i + 1] = ((v >> 5) & 0x3F) << 2;
		dst[3 * i + 2] = (v & 0x1F) << 3;
	}
}

// The top framebuffer is 240x400 BGR8, rotated: each screen column x is a run
// of 240 pixels stored bottom-to-top. Landscape pixel (x, y) is at
// ((x * TOP_H) + (TOP_H - 1 - y)) * 3.
static void draw_frame(u8 *fb, const u8 *rgb)
{
	// Clear the side bars (both buffers of a double-buffered screen need it).
	memset(fb, 0, X_OFF * TOP_H * 3);
	memset(fb + (X_OFF + CAM_W) * TOP_H * 3, 0, (TOP_W - X_OFF - CAM_W) * TOP_H * 3);

	for (int x = 0; x < CAM_W; x++) {
		u8 *col = fb + (x + X_OFF) * TOP_H * 3;
		for (int y = 0; y < CAM_H; y++) {
			const u8 *p = rgb + (y * CAM_W + x) * 3;
			u8 *q = col + (TOP_H - 1 - y) * 3;
			q[0] = p[2];
			q[1] = p[1];
			q[2] = p[0];
		}
	}
}

static void put_px(u8 *fb, int x, int y, u8 r, u8 g, u8 b)
{
	u8 *q = fb + ((x + X_OFF) * TOP_H + (TOP_H - 1 - y)) * 3;
	q[0] = b; q[1] = g; q[2] = r;
}

static void draw_roi(u8 *fb)
{
	int x0 = ROI_X(CAM_W), y0 = ROI_Y(CAM_H);
	int x1 = x0 + ROI_W(CAM_W) - 1, y1 = y0 + ROI_H(CAM_H) - 1;
	for (int x = x0; x <= x1; x++) {
		put_px(fb, x, y0, 0, 255, 0);
		put_px(fb, x, y1, 0, 255, 0);
	}
	for (int y = y0; y <= y1; y++) {
		put_px(fb, x0, y, 0, 255, 0);
		put_px(fb, x1, y, 0, 255, 0);
	}
}

// ------------------------------------------------------------------ LARR-E API link
//
// A background thread polls GET /api/screen every POLL_MS and posts approve or
// deny decisions, so a slow or missing network never stalls the camera.

#define POLL_MS 2000
#define HTTP_TIMEOUT_NS 5000000000ULL // 5 s
#define BODY_MAX 4096

static larre_cfg cfg;
static bool have_cfg;

static LightLock link_lock;            // guards everything below it
static larre_screen link_screen;
static char link_status[SCREEN_COLS + 1] = "connecting...";
static int link_generation;            // bumped whenever the above changes
static char decide_id[APPROVAL_ID_MAX + 1]; // set by the main thread, sent by the link thread
static bool decide_approve;

static volatile bool link_quit;
static LightEvent link_wake;

static bool load_cfg_from(const char *path)
{
	FILE *f = fopen(path, "r");
	if (!f)
		return false;
	char text[512];
	size_t n = fread(text, 1, sizeof(text) - 1, f);
	fclose(f);
	text[n] = '\0';
	return cfg_parse(text, &cfg) == 0;
}

// The SD card's file wins, so a bundled build can be pointed elsewhere without rebuilding.
static bool load_cfg(void)
{
	if (load_cfg_from(LARRE_CFG_PATH))
		return true;
	bool ok = false;
	if (R_SUCCEEDED(romfsInit())) {
		ok = load_cfg_from(LARRE_CFG_BUNDLED);
		romfsExit();
	}
	return ok;
}

// One HTTP request. Fills *status with the HTTP status code and, when out is
// given, out with the (possibly truncated) body.
static Result http_request(HTTPC_RequestMethod method, const char *url, const char *json,
                           char *out, u32 out_size, u32 *status)
{
	static u32 post_buf[64]; // httpcAddPostDataRaw wants word-aligned data
	httpcContext ctx;
	*status = 0;
	Result r = httpcOpenContext(&ctx, method, url, 1);
	if (R_FAILED(r))
		return r;
	httpcSetKeepAlive(&ctx, HTTPC_KEEPALIVE_DISABLED);
	httpcAddRequestHeaderField(&ctx, "User-Agent", "larre-cam");
	httpcAddRequestHeaderField(&ctx, "X-Larre-Token", cfg.token);
	if (json) {
		u32 len = strlen(json);
		if (len > sizeof(post_buf)) {
			httpcCloseContext(&ctx);
			return -1;
		}
		memcpy(post_buf, json, len);
		httpcAddRequestHeaderField(&ctx, "Content-Type", "application/json");
		httpcAddPostDataRaw(&ctx, post_buf, len);
	}
	r = httpcBeginRequest(&ctx);
	if (R_SUCCEEDED(r))
		r = httpcGetResponseStatusCodeTimeout(&ctx, status, HTTP_TIMEOUT_NS);
	if (R_SUCCEEDED(r) && out) {
		u32 got = 0;
		r = httpcDownloadData(&ctx, (u8 *)out, out_size - 1, &got);
		if (r == (Result)HTTPC_RESULTCODE_DOWNLOADPENDING)
			r = 0; // more than fits: keep what we have
		out[got] = '\0';
	}
	httpcCloseContext(&ctx);
	return r;
}

static void set_status(const char *text)
{
	LightLock_Lock(&link_lock);
	snprintf(link_status, sizeof(link_status), "%s", text);
	link_generation++;
	LightLock_Unlock(&link_lock);
}

static void link_thread(void *arg)
{
	(void)arg;
	static char body[BODY_MAX];
	static larre_screen fresh;
	char url[sizeof(cfg.url) + 64], msg[SCREEN_COLS + 1];
	u32 code;

	while (!link_quit) {
		char id[APPROVAL_ID_MAX + 1];
		bool approve;
		LightLock_Lock(&link_lock);
		snprintf(id, sizeof(id), "%s", decide_id);
		approve = decide_approve;
		decide_id[0] = '\0';
		LightLock_Unlock(&link_lock);

		if (id[0]) {
			snprintf(url, sizeof(url), "%s/api/approvals/%s", cfg.url, id);
			Result r = http_request(HTTPC_METHOD_POST, url,
				approve ? "{\"decision\":\"approve\",\"by\":\"3ds\"}" : "{\"decision\":\"deny\",\"by\":\"3ds\"}",
				NULL, 0, &code);
			if (R_SUCCEEDED(r) && code == 200)
				snprintf(msg, sizeof(msg), "%s", approve ? "approved" : "denied");
			else if (R_SUCCEEDED(r))
				snprintf(msg, sizeof(msg), "decision refused: HTTP %lu", (unsigned long)code);
			else
				snprintf(msg, sizeof(msg), "decision not sent: %08lX", (unsigned long)r);
			set_status(msg);
		}

		snprintf(url, sizeof(url), "%s/api/screen?cols=%d", cfg.url, SCREEN_COLS);
		Result r = http_request(HTTPC_METHOD_GET, url, NULL, body, sizeof(body), &code);
		if (R_SUCCEEDED(r) && code == 200) {
			screen_parse(body, &fresh);
			LightLock_Lock(&link_lock);
			link_screen = fresh;
			if (!id[0])
				snprintf(link_status, sizeof(link_status), "online");
			link_generation++;
			LightLock_Unlock(&link_lock);
		} else {
			if (R_SUCCEEDED(r))
				if (code == 401)
					snprintf(msg, sizeof(msg), "wrong token (HTTP 401)");
				else
					snprintf(msg, sizeof(msg), "HTTP %lu", (unsigned long)code);
			else
				snprintf(msg, sizeof(msg), "no connection (%08lX)", (unsigned long)r);
			set_status(msg);
		}
		LightEvent_WaitTimeout(&link_wake, POLL_MS * 1000000LL);
	}
}

// ------------------------------------------------------------------ bottom screen
//
// The console is 40x30. Row 1: title and link state. Row 2: camera reading.
// Rows 4-23: LARR-E's screen lines. Row 29: the approval prompt. Row 30: help.

#define ROW_LINES 4
#define ROW_PROMPT 29
#define ROW_HELP 30

static void draw_line(int row, const char *colour, const char *text)
{
	// A full row on the last line would scroll the console, so that one stops a column short.
	int w = row == ROW_HELP ? SCREEN_COLS - 1 : SCREEN_COLS;
	printf("\x1b[%d;1H%s%-*.*s\x1b[0m", row, colour, w, w, text);
}

static void draw_link(void)
{
	static int drawn = -1;
	LightLock_Lock(&link_lock);
	if (drawn == link_generation) {
		LightLock_Unlock(&link_lock);
		return;
	}
	drawn = link_generation;
	larre_screen s = link_screen;
	char status[SCREEN_COLS + 1];
	snprintf(status, sizeof(status), "%s", link_status);
	LightLock_Unlock(&link_lock);

	char title[SCREEN_COLS + 1];
	snprintf(title, sizeof(title), "LARR-E Cam  %.28s", status);
	draw_line(1, "", title);
	for (int i = 0; i < SCREEN_MAX_LINES; i++) {
		const char *colour = "";
		if (i < s.n && s.tag[i] == 'C')
			colour = "\x1b[31m"; // red: critical alert
		else if (i < s.n && (s.tag[i] == 'A' || s.tag[i] == 'P'))
			colour = "\x1b[33m"; // yellow: alert or approval title
		draw_line(ROW_LINES + i, colour, i < s.n ? s.text[i] : "");
	}
	draw_line(ROW_PROMPT, "\x1b[33m", s.approval_id[0] ? "A: approve   B: deny" : "");
}

static void request_decision(bool approve)
{
	LightLock_Lock(&link_lock);
	if (link_screen.approval_id[0]) { // only the action that is on screen now
		snprintf(decide_id, sizeof(decide_id), "%s", link_screen.approval_id);
		decide_approve = approve;
		snprintf(link_status, sizeof(link_status), "sending...");
		link_generation++;
	}
	LightLock_Unlock(&link_lock);
	LightEvent_Signal(&link_wake);
}

int main(void)
{
	gfxInitDefault();
	consoleInit(GFX_BOTTOM, NULL);
	gfxSetDoubleBuffering(GFX_TOP, true);
	osSetSpeedupEnable(true); // New 3DS: 804 MHz and the extra L2 cache

	LightLock_Init(&link_lock);
	LightEvent_Init(&link_wake, RESET_ONESHOT);
	Thread link = NULL;
	have_cfg = load_cfg();
	if (have_cfg && R_SUCCEEDED(httpcInit(0))) {
		s32 prio = 0x30;
		svcGetThreadPriority(&prio, CUR_THREAD_HANDLE);
		link = threadCreate(link_thread, NULL, 32 * 1024, prio + 1, -2, false);
	}
	if (!link) {
		snprintf(link_status, sizeof(link_status), "%s", have_cfg ? "offline (no HTTP)" : "offline");
		if (!have_cfg) {
			link_screen.n = 3;
			snprintf(link_screen.text[0], SCREEN_COLS + 1, "No LARR-E link. To add one, put");
			snprintf(link_screen.text[1], SCREEN_COLS + 1, "url=http://<laptop-ip>:8787 and");
			snprintf(link_screen.text[2], SCREEN_COLS + 1, "token=<token> in %s", "/3ds/larre.cfg");
		}
	}
	draw_line(ROW_HELP, "", "Aim the box at the vial.  START: exit");

	CHECK(camInit());
	CHECK(CAMU_SetSize(SELECT_OUT1, SIZE_QVGA, CONTEXT_A));
	CHECK(CAMU_SetOutputFormat(SELECT_OUT1, OUTPUT_RGB_565, CONTEXT_A));
	CHECK(CAMU_SetFrameRate(SELECT_OUT1, FRAME_RATE_30));
	CHECK(CAMU_SetNoiseFilter(SELECT_OUT1, true));
	CHECK(CAMU_SetAutoExposure(SELECT_OUT1, true));
	CHECK(CAMU_SetAutoWhiteBalance(SELECT_OUT1, true));
	CHECK(CAMU_SetTrimming(PORT_CAM1, false));

	u32 transfer = 0;
	CHECK(CAMU_GetMaxBytes(&transfer, CAM_W, CAM_H));
	CHECK(CAMU_SetTransferBytes(PORT_CAM1, transfer, CAM_W, CAM_H));
	CHECK(CAMU_Activate(SELECT_OUT1));

	// ev[0]: buffer error (capture interrupted), ev[1]: frame received.
	Handle ev[2] = {0, 0};
	CHECK(CAMU_GetBufferErrorInterruptEvent(&ev[0], PORT_CAM1));
	CHECK(CAMU_ClearBuffer(PORT_CAM1));
	CHECK(CAMU_StartCapture(PORT_CAM1));

	bool interrupted = false;
	u64 fps_t0 = osGetTime();
	int fps_frames = 0;
	float fps = 0.0f;
	TickCounter tc;

	while (aptMainLoop()) {
		hidScanInput();
		u32 down = hidKeysDown();
		if (down & KEY_START)
			break;
		if (link && (down & (KEY_A | KEY_B)))
			request_decision(down & KEY_A);

		if (ev[1] == 0)
			CHECK(CAMU_SetReceiving(&ev[1], cam_buf, PORT_CAM1, CAM_BYTES, (s16)transfer));
		if (interrupted) {
			CHECK(CAMU_StartCapture(PORT_CAM1));
			interrupted = false;
		}

		s32 which = -1;
		Result r = svcWaitSynchronizationN(&which, ev, 2, false, WAIT_TIMEOUT);
		if (R_FAILED(r) || which < 0)
			continue; // timeout: try again
		svcCloseHandle(ev[1]);
		ev[1] = 0;
		if (which == 0) {
			interrupted = true;
			continue;
		}

		rgb565_to_rgb888(cam_buf, rgb_buf, CAM_W * CAM_H);

		osTickCounterStart(&tc);
		int score = classify_frame(rgb_buf, CAM_W, CAM_H);
		osTickCounterUpdate(&tc);
		double classify_ms = osTickCounterRead(&tc);

		u8 *fb = gfxGetFramebuffer(GFX_TOP, GFX_LEFT, NULL, NULL);
		draw_frame(fb, rgb_buf);
		draw_roi(fb);
		gfxFlushBuffers();
		gfxSwapBuffers();
		gspWaitForVBlank();

		fps_frames++;
		u64 now = osGetTime();
		if (now - fps_t0 >= 1000) {
			fps = fps_frames * 1000.0f / (float)(now - fps_t0);
			fps_frames = 0;
			fps_t0 = now;
		}

		char cam[SCREEN_COLS + 1];
		snprintf(cam, sizeof(cam), "bright %3d  fps %4.1f  classify %4.1fms", score, fps, classify_ms);
		draw_line(2, "", cam);
		draw_link();
	}
	if (link) {
		link_quit = true;
		LightEvent_Signal(&link_wake);
		threadJoin(link, U64_MAX);
		threadFree(link);
		httpcExit();
	}

	CHECK(CAMU_StopCapture(PORT_CAM1));
	for (int i = 0; i < 2; i++)
		if (ev[i])
			svcCloseHandle(ev[i]);
	CHECK(CAMU_Activate(SELECT_NONE));
	camExit();
	gfxExit();
	return 0;
}
