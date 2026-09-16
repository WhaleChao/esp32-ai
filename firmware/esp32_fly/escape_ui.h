#ifndef FLY_ESCAPE_UI_H
#define FLY_ESCAPE_UI_H
#include <Arduino.h>
#include "esp_timer.h"
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SH110X.h>
#include "escape_world.h"

// The display task owns world ticks and OLED writes after setup. The Arduino
// loop samples loom levels and applies escape decisions while holding game_mutex.
static Adafruit_SH1106G oled(128, 64, &Wire, -1);
static SemaphoreHandle_t game_mutex;
static EscapeWorld world;
static bool display_present = false, game_active = false;
static uint8_t display_address = 0;
static uint64_t game_epoch_us = 0;
static uint32_t display_frames = 0, neural_steps = 0;
static uint64_t last_graph_us = 0, last_cycle_us = 0;
static float last_looms[2] = {0, 0}, last_drives[2] = {0, 0};
static char game_error[48] = {0};
#define UI_STATUS_BYTES 640

static void ui_error(const char *reason) {
    if (!game_mutex)
        return;
    xSemaphoreTake(game_mutex, portMAX_DELAY);
    game_active = false;
    snprintf(game_error, sizeof(game_error), "%s", reason);
    xSemaphoreGive(game_mutex);
}

static bool ui_begin() {
    game_mutex = xSemaphoreCreateMutex();
    if (!game_mutex)
        return false;
    Wire.begin(18, 46);
    Wire.setClock(400000);
    Wire.setTimeOut(50);
    for (uint8_t address = 0x3c; address <= 0x3d; address++) {
        Wire.beginTransmission(address);
        if (Wire.endTransmission() == 0) {
            display_address = address;
            break;
        }
    }
    if (!display_address)
        return false;
    display_present = oled.begin(display_address, true);
    if (display_present) {
        oled.clearDisplay();
        oled.setTextColor(SH110X_WHITE);
        oled.setTextSize(1);
        oled.setCursor(34, 12);
        oled.print("FLY ESCAPE");
        oled.setCursor(16, 32);
        oled.print("Loading graph...");
        oled.display();
    }
    return display_present;
}
static void ui_stop() {
    if (game_mutex) {
        xSemaphoreTake(game_mutex, portMAX_DELAY);
        game_active = false;
        xSemaphoreGive(game_mutex);
    }
}
static bool ui_active() {
    bool active = false;
    if (game_mutex) {
        xSemaphoreTake(game_mutex, portMAX_DELAY);
        active = game_active;
        xSemaphoreGive(game_mutex);
    }
    return active;
}

// Formats one "escape" status event; returns the snprintf length.
static int ui_format_status(char *line, size_t size) {
    EscapeWorld w;
    float looms[2], drives[2];
    xSemaphoreTake(game_mutex, portMAX_DELAY);
    w = world;
    bool active = game_active;
    uint32_t frames = display_frames;
    memcpy(looms, last_looms, sizeof(looms));
    memcpy(drives, last_drives, sizeof(drives));
    xSemaphoreGive(game_mutex);
    return snprintf(
        line, size,
        "{\"event\":\"escape\",\"active\":%s,\"display_present\":%s,\"display_addr\":%u,\"sda\":18,"
        "\"scl\":46,\"escapes\":%u,\"caught\":%u,\"encounters\":%u,\"fly\":[%.3f,%.3f,%.4f],"
        "\"fly_state\":%d,\"spider\":[%.3f,%.3f],\"spider_state\":%d,\"ticks\":%llu,\"frames\":%u,"
        "\"neural_steps\":%u,\"looms\":[%.9g,%.9g],\"drives\":[%.9g,%.9g],\"graph_us\":%llu,"
        "\"cycle_us\":%llu}\n",
        active ? "true" : "false", display_present ? "true" : "false", (unsigned)display_address,
        (unsigned)w.escapes, (unsigned)w.caught, (unsigned)w.encounters, w.fly_x, w.fly_y,
        w.fly_heading, w.fly_state, w.spider_x, w.spider_y, w.spider_state,
        (unsigned long long)w.ticks, (unsigned)frames, (unsigned)neural_steps, looms[0], looms[1],
        drives[0], drives[1], (unsigned long long)last_graph_us, (unsigned long long)last_cycle_us);
}
static void ui_status() {
    char line[UI_STATUS_BYTES];
    int length = ui_format_status(line, sizeof(line));
    if (length > 0 && (size_t)length < sizeof(line))
        Serial.write((const uint8_t *)line, (size_t)length);
}

// A fly seen from above: body, head toward the heading, and two wings that
// flutter while walking and spread during a jump.
static void draw_fly(const EscapeWorld *w, uint32_t frame) {
    int x = (int)lround(w->fly_x), y = (int)lround(w->fly_y);
    double hx = cos(w->fly_heading), hy = sin(w->fly_heading);
    oled.fillCircle(x, y, 1, SH110X_WHITE);
    oled.drawPixel(x + (int)lround(2 * hx), y + (int)lround(2 * hy), SH110X_WHITE);
    int span = (w->fly_state == FLY_JUMP || frame % 2) ? 3 : 2;
    for (int s = -1; s <= 1; s += 2)
        oled.drawLine(x, y, x + (int)lround(-hx - s * span * hy), y + (int)lround(-hy + s * span * hx),
                      SH110X_WHITE);
    if (w->fly_state == FLY_JUMP)
        oled.drawLine(x - (int)lround(3 * hx), y - (int)lround(3 * hy), x - (int)lround(6 * hx),
                      y - (int)lround(6 * hy), SH110X_WHITE);
}

// A spider: round body and four legs on each side that wiggle as it walks.
static void draw_spider(const EscapeWorld *w, uint32_t frame) {
    if (w->spider_state == SPIDER_ABSENT)
        return;
    int x = (int)lround(w->spider_x), y = (int)lround(w->spider_y);
    oled.fillCircle(x, y, 2, SH110X_WHITE);
    for (int k = 0; k < 4; k++) {
        double angle = -0.9 + 0.6 * k + (((frame / 2) + k) % 2 ? 0.15 : -0.15);
        int dx = (int)lround(5 * cos(angle)), dy = (int)lround(5 * sin(angle));
        oled.drawLine(x, y, x + dx, y + dy, SH110X_WHITE);
        oled.drawLine(x, y, x - dx, y + dy, SH110X_WHITE);
    }
}

// Escape-neuron activity for one side, drawn at that side of the screen so a
// rising bar sits where the danger is. The marker in the middle is the threshold.
static void draw_drive(int x, float drive, float threshold) {
    const int y = 1, width = 22;
    oled.drawRect(x, y, width, 6, SH110X_WHITE);
    int fill = (int)lround(fmin(1.0, fmax(0.0, drive / (2.0 * threshold))) * (width - 2));
    oled.fillRect(x + 1, y + 1, fill, 4, SH110X_WHITE);
    oled.drawFastVLine(x + width / 2, y, 6, SH110X_BLACK);
}

static void ui_task(void *threshold_argument) {
    const float threshold = *(const float *)threshold_argument;
    uint64_t last_frame = 0;
    for (;;) {
        uint64_t t = esp_timer_get_time();
        EscapeWorld copy;
        bool active;
        float drives[2];
        char error[sizeof(game_error)];
        xSemaphoreTake(game_mutex, portMAX_DELAY);
        active = game_active;
        // Read time after taking the lock: PLAY may have just replaced the epoch.
        if (active) {
            uint64_t due = (esp_timer_get_time() - game_epoch_us) * WORLD_HZ / 1000000;
            while (world.ticks < due)
                world_tick(&world);
        }
        copy = world;
        memcpy(drives, last_drives, sizeof(drives));
        memcpy(error, game_error, sizeof(error));
        xSemaphoreGive(game_mutex);
        if (display_present && t - last_frame >= 50000) {
            uint32_t frame = display_frames;
            oled.clearDisplay();
            draw_spider(&copy, frame);
            draw_fly(&copy, frame);
            // The header is drawn last so sprites near the top edge never cover it.
            oled.fillRect(0, 0, 128, 12, SH110X_BLACK);
            oled.setTextSize(1);
            oled.setTextColor(SH110X_WHITE);
            char counters[24];
            // Capped so the text never reaches the activity bars.
            snprintf(counters, sizeof(counters), "%u/%u",
                     copy.escapes < 999u ? (unsigned)copy.escapes : 999u,
                     copy.caught < 999u ? (unsigned)copy.caught : 999u);
            oled.setCursor(64 - (int)strlen(counters) * 3, 1);
            oled.print(counters);
            draw_drive(2, drives[0], threshold);
            draw_drive(104, drives[1], threshold);
            oled.drawFastHLine(0, 11, 128, SH110X_WHITE);
            if (!active) {
                oled.setCursor(46, 30);
                oled.print("PAUSED");
            }
            if (error[0]) {
                oled.fillRect(0, 18, 128, 30, SH110X_BLACK);
                oled.setCursor(0, 20);
                oled.print(error);
            }
            oled.display();
            xSemaphoreTake(game_mutex, portMAX_DELAY);
            display_frames++;
            xSemaphoreGive(game_mutex);
            last_frame = t;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}
#endif
