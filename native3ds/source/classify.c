#include "classify.h"

int classify_frame(const uint8_t *rgb, int w, int h)
{
	int x0 = ROI_X(w), y0 = ROI_Y(h);
	int rw = ROI_W(w), rh = ROI_H(h);
	uint32_t sum = 0;

	for (int y = y0; y < y0 + rh; y++) {
		const uint8_t *p = rgb + (y * w + x0) * 3;
		for (int x = 0; x < rw; x++, p += 3) {
			// Integer luma, BT.601 weights scaled to 256.
			sum += (77 * p[0] + 150 * p[1] + 29 * p[2]) >> 8;
		}
	}
	return (int)(sum / (uint32_t)(rw * rh));
}
