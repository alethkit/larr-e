// LARR-E Cam: live outer-camera feed on the top screen, a classifier score
// and fps on the bottom screen.
//
// Camera setup follows devkitPro's 3ds-examples camera/video example, cut
// down to one camera (outer right, OUT1) at QVGA (320x240).

#include <stdio.h>
#include <string.h>
#include <3ds.h>

#include "classify.h"

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

int main(void)
{
	gfxInitDefault();
	consoleInit(GFX_BOTTOM, NULL);
	gfxSetDoubleBuffering(GFX_TOP, true);
	osSetSpeedupEnable(true); // New 3DS: 804 MHz and the extra L2 cache

	printf("\x1b[1;1HLARR-E Cam\n");
	printf("Outer camera, %dx%d RGB565\n", CAM_W, CAM_H);
	printf("Aim the green box at the vial.\n");
	printf("START: exit\n");

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
		if (hidKeysDown() & KEY_START)
			break;

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

		printf("\x1b[7;1Hfps:        %5.1f   \n", fps);
		printf("brightness: %5d   (0-255, centre box)\n", score);
		printf("classify:   %5.2f ms\n", classify_ms);
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
