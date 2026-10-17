#pragma once

#include <stdint.h>

// Centre region the classifier looks at: the middle half of the frame in each
// direction. main.c draws this box on the top screen so you can aim the vial.
#define ROI_X(w) ((w) / 4)
#define ROI_Y(h) ((h) / 4)
#define ROI_W(w) ((w) / 2)
#define ROI_H(h) ((h) / 2)

// Look at one camera frame and return a score.
//
// rgb: w*h pixels, 3 bytes each (R, G, B), row-major, top-left first.
//
// Today: the mean brightness (0-255) of the centre region, a crude turbidity
// proxy (a cloudy culture scatters more light than clear medium). Step 2
// replaces the body with an int8 CNN (TFLite Micro) and returns a class.
int classify_frame(const uint8_t *rgb, int w, int h);
