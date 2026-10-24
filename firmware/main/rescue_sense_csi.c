/*
 * RescueSense CSI receiver (ESP32-S3)
 *
 * Derived from Espressif's official esp-csi examples:
 *   examples/get-started/csi_recv_router/main/app_main.c
 *   examples/get-started/csi_recv/main/app_main.c
 * SPDX-License-Identifier: CC0-1.0
 *
 * Router mode (default) joins a 2.4 GHz AP and pings its gateway at
 * CONFIG_RESCUE_PING_HZ; CSI is taken from the AP's replies.  Direct mode is
 * for Espressif's stock csi_send running on a second ESP32.
 *
 * Wire protocol (one line per CSI packet, console port):
 *   CSI,<timestamp_us>,<rssi_dbm>,<amp_sc0>,<amp_sc1>,...
 * Amplitudes carry one decimal place.  Status lines start with '#'.
 * Host commands (one per line): ALERT,1  ALERT,0  STATUS  PING
 */

#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#elif CONFIG_ESP_CONSOLE_UART
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#endif

#if CONFIG_RESCUE_ROUTER_MODE
#include "lwip/ip4_addr.h"
#include "ping/ping_sock.h"
#else
#include "esp_now.h"
#endif

#define TAG "rescuesense"
#define FIRMWARE_VERSION "2.0"

/* LLTF-only CSI is 64 subcarriers (128 bytes) on HT20 and 128 subcarriers
 * (256 bytes) when an HT40 packet slips through. */
#define CSI_BYTES_MAX 256
#define CSI_AMPLITUDES_MAX 128
#define CSI_QUEUE_DEPTH 32
#define CSI_LINE_MAX 1200

/* Espressif's examples record 100 packets of AGC/FFT gain before the
 * compensation factor is meaningful; until then the component returns
 * ESP_ERR_INVALID_STATE and amplitudes would jump with every AGC step. */
#define GAIN_BASELINE_PACKETS 100
#define GAIN_BASELINE_GIVE_UP 600
#define STATS_INTERVAL_US 2000000

#if defined(WIFI_CSI_PHY_GAIN_ENABLE) && WIFI_CSI_PHY_GAIN_ENABLE
#define RESCUE_HAS_GAIN_CTRL 1
#else
#define RESCUE_HAS_GAIN_CTRL 0
#endif

#if CONFIG_RESCUE_GAIN_FORCE
#define GAIN_MODE_NAME "force"
#elif CONFIG_RESCUE_GAIN_OFF
#define GAIN_MODE_NAME "off"
#else
#define GAIN_MODE_NAME "compensate"
#endif

/* Only the optional two-board ESP-NOW mode uses these addresses. Keeping
 * them out of the router build avoids unused-constant errors under -Werror. */
#if !CONFIG_RESCUE_ROUTER_MODE
static const uint8_t DIRECT_TX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const uint8_t DIRECT_RX_MAC[6] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x01};
#endif

typedef struct {
    uint64_t timestamp_us;
    uint16_t len;
    int8_t rssi;
    bool first_word_invalid;
    float gain_compensation;
    int8_t bytes[CSI_BYTES_MAX];
} csi_frame_t;

static QueueHandle_t s_csi_queue;
static uint8_t s_expected_sender[6];
static volatile bool s_alert_active;
static volatile int64_t s_last_command_us;
static volatile uint32_t s_dropped_frames;
static volatile uint32_t s_received_frames;

static volatile bool s_gain_reset_requested;
static volatile bool s_gain_ready;
static volatile uint32_t s_gain_samples;
static volatile uint8_t s_last_agc;
static volatile int8_t s_last_fft;
static volatile uint8_t s_base_agc;
static volatile int8_t s_base_fft;
static volatile float s_last_gain = 1.0f;

static char s_line[CSI_LINE_MAX];

static void init_nvs(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

/*
 * Without a driver, ESP-IDF's console stdin is non-blocking: fgets() returns
 * NULL whenever the FIFO is empty and the stream's error flag sticks, so
 * ALERT commands were silently lost.  Installing the driver makes stdin a
 * normal blocking stream on whichever console the sdkconfig selected.
 */
static void init_console_io(void)
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_driver_config_t usb_config = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_config.rx_buffer_size = 512;
    usb_config.tx_buffer_size = 4096;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb_config));
    usb_serial_jtag_vfs_use_driver();
#elif CONFIG_ESP_CONSOLE_UART
    ESP_ERROR_CHECK(uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, 1024, 0, 0, NULL, 0));
    uart_vfs_dev_use_driver(CONFIG_ESP_CONSOLE_UART_NUM);
#endif
}

static void alert_task(void *arg)
{
    bool on = false;
    while (true) {
        if (s_alert_active && CONFIG_RESCUE_ALERT_TIMEOUT_S > 0
                && esp_timer_get_time() - s_last_command_us > (int64_t)CONFIG_RESCUE_ALERT_TIMEOUT_S * 1000000) {
            /* The laptop refreshes the alert state every few seconds.  If it
             * crashes or the cable is pulled, do not leave the buzzer on. */
            s_alert_active = false;
            printf("#ALERT,0,timeout\n");
            fflush(stdout);
        }
        if (s_alert_active) {
            on = !on;
            gpio_set_level(CONFIG_RESCUE_BUZZER_GPIO, on ? 1 : 0);
            vTaskDelay(pdMS_TO_TICKS(125));
        } else {
            on = false;
            gpio_set_level(CONFIG_RESCUE_BUZZER_GPIO, 0);
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
}

static void init_alert_gpio(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << CONFIG_RESCUE_BUZZER_GPIO),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io_conf));
    gpio_set_level(CONFIG_RESCUE_BUZZER_GPIO, 0);
}

static void print_status(void)
{
    printf("#STATUS,fw=" FIRMWARE_VERSION ",alert=%d,received=%" PRIu32 ",dropped=%" PRIu32
           ",gain=" GAIN_MODE_NAME ",gain_ready=%d,agc=%u,fft=%d\n",
           s_alert_active ? 1 : 0, s_received_frames, s_dropped_frames,
           s_gain_ready ? 1 : 0, (unsigned)s_last_agc, (int)s_last_fft);
    fflush(stdout);
}

/* The backend writes ALERT,1 / ALERT,0 (and a periodic heartbeat) here. */
static void serial_command_task(void *arg)
{
    char line[48];
    while (true) {
        if (fgets(line, sizeof(line), stdin) == NULL) {
            clearerr(stdin);
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        s_last_command_us = esp_timer_get_time();

        if (strncmp(line, "ALERT,1", 7) == 0) {
            if (!s_alert_active) {
                printf("#ALERT,1\n");
                fflush(stdout);
            }
            s_alert_active = true;
        } else if (strncmp(line, "ALERT,0", 7) == 0) {
            if (s_alert_active) {
                printf("#ALERT,0\n");
                fflush(stdout);
            }
            s_alert_active = false;
        } else if (strncmp(line, "STATUS", 6) == 0) {
            print_status();
        } else if (strncmp(line, "PING", 4) == 0) {
            printf("#PONG\n");
            fflush(stdout);
        }
    }
}

/*
 * Keep this callback short: ESP-IDF runs it in the Wi-Fi task.  Magnitude
 * conversion and UART I/O happen in csi_print_task instead.
 */
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (info == NULL || info->buf == NULL || info->len == 0) {
        return;
    }
    if (memcmp(info->mac, s_expected_sender, sizeof(s_expected_sender)) != 0) {
        return;
    }

    float gain = 1.0f;
#if RESCUE_HAS_GAIN_CTRL
    if (s_gain_reset_requested) {
        /* A new association (possibly a different AP) needs a new reference. */
        s_gain_reset_requested = false;
        s_gain_ready = false;
        s_gain_samples = 0;
#if CONFIG_RESCUE_GAIN_FORCE
        esp_csi_gain_ctrl_set_rx_force_gain(0, 0);
#endif
        esp_csi_gain_ctrl_reset_rx_gain_baseline();
    }

    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc_gain, &fft_gain);
    s_last_agc = agc_gain;
    s_last_fft = fft_gain;

    if (!s_gain_ready) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
        uint32_t samples = s_gain_samples + 1;
        s_gain_samples = samples;
        if (samples >= GAIN_BASELINE_PACKETS) {
            uint8_t base_agc = 0;
            int8_t base_fft = 0;
            if (esp_csi_gain_ctrl_get_rx_gain_baseline(&base_agc, &base_fft) == ESP_OK) {
#if CONFIG_RESCUE_GAIN_FORCE
                esp_csi_gain_ctrl_set_rx_force_gain(base_agc, base_fft);
#endif
                s_base_agc = base_agc;
                s_base_fft = base_fft;
                s_gain_ready = true;
            } else if (samples >= GAIN_BASELINE_GIVE_UP) {
                /* Never stall the stream: fall back to raw amplitudes. */
                s_gain_ready = true;
            }
        }
        /* Emit nothing until the reference exists, otherwise the first
         * second of every capture contains an artificial amplitude step. */
        return;
    }

#if CONFIG_RESCUE_GAIN_COMPENSATE
    if (esp_csi_gain_ctrl_get_gain_compensation(&gain, agc_gain, fft_gain) != ESP_OK
            || !isfinite(gain) || gain <= 0.0f || gain > 16.0f) {
        gain = 1.0f;
    }
#endif
#endif /* RESCUE_HAS_GAIN_CTRL */
    s_last_gain = gain;

    csi_frame_t frame = {
        .timestamp_us = (uint64_t)esp_timer_get_time(),
        .len = info->len > CSI_BYTES_MAX ? CSI_BYTES_MAX : info->len,
        .rssi = info->rx_ctrl.rssi,
        .first_word_invalid = info->first_word_invalid,
        .gain_compensation = gain,
    };
    memcpy(frame.bytes, info->buf, frame.len);
    ++s_received_frames;
    if (xQueueSend(s_csi_queue, &frame, 0) != pdPASS) {
        ++s_dropped_frames;
    }
}

static size_t append_u32(char *dst, uint32_t value)
{
    char reversed[10];
    size_t count = 0;
    do {
        reversed[count++] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value != 0 && count < sizeof(reversed));
    for (size_t i = 0; i < count; ++i) {
        dst[i] = reversed[count - 1 - i];
    }
    return count;
}

/*
 * Builds the whole row in one buffer and writes it with a single fwrite.
 * The previous version issued ~65 printf calls per packet and rounded every
 * amplitude to an integer, discarding most of a breathing-sized change.
 */
static size_t format_frame(const csi_frame_t *frame, char *out, size_t capacity)
{
    int written = snprintf(out, capacity, "CSI,%" PRIu64 ",%d", frame->timestamp_us, (int)frame->rssi);
    size_t pos = written > 0 ? (size_t)written : 0;

    uint16_t count = frame->len / 2;
    if (count > CSI_AMPLITUDES_MAX) {
        count = CSI_AMPLITUDES_MAX;
    }
    for (uint16_t sc = 0; sc < count && pos + 16 < capacity; ++sc) {
        uint32_t tenths = 0;
        /* ESP32-S3 may mark the first four bytes invalid.  Emit zeros so the
         * row width stays constant; the host masks those bins anyway. */
        if (!(frame->first_word_invalid && sc < 2)) {
            float imag = (float)frame->bytes[2 * sc];
            float real = (float)frame->bytes[2 * sc + 1];
            float scaled = sqrtf(real * real + imag * imag) * frame->gain_compensation * 10.0f;
            tenths = scaled >= 99999.0f ? 99999u : (uint32_t)lroundf(scaled);
        }
        out[pos++] = ',';
        pos += append_u32(out + pos, tenths / 10u);
        out[pos++] = '.';
        out[pos++] = (char)('0' + tenths % 10u);
    }
    out[pos++] = '\n';
    return pos;
}

static void csi_print_task(void *arg)
{
    csi_frame_t frame;
    int64_t window_start_us = esp_timer_get_time();
    uint32_t printed_in_window = 0;
    bool announced_gain = false;

    while (true) {
        if (xQueueReceive(s_csi_queue, &frame, pdMS_TO_TICKS(200)) == pdPASS) {
            size_t length = format_frame(&frame, s_line, sizeof(s_line));
            fwrite(s_line, 1, length, stdout);
            fflush(stdout);
            ++printed_in_window;
        }

        if (s_gain_ready && !announced_gain) {
            announced_gain = true;
            printf("#GAIN,mode=" GAIN_MODE_NAME ",base_agc=%u,base_fft=%d\n",
                   (unsigned)s_base_agc, (int)s_base_fft);
            fflush(stdout);
        } else if (!s_gain_ready) {
            announced_gain = false;
        }

        int64_t now = esp_timer_get_time();
        if (now - window_start_us >= STATS_INTERVAL_US) {
            float rate = (float)printed_in_window * 1.0e6f / (float)(now - window_start_us);
            printf("#STAT,pps=%.1f,dropped=%" PRIu32 ",gain=" GAIN_MODE_NAME ",gain_ready=%d,agc=%u,fft=%d,"
                   "comp=%.3f,alert=%d\n",
                   (double)rate, s_dropped_frames, s_gain_ready ? 1 : 0,
                   (unsigned)s_last_agc, (int)s_last_fft, (double)s_last_gain, s_alert_active ? 1 : 0);
            fflush(stdout);
            window_start_us = now;
            printed_in_window = 0;
        }
    }
}

static void wifi_csi_init(void)
{
    /* Router mode already receives AP replies as an associated STA. Keeping
     * promiscuous mode off prevents unrelated AP traffic from entering the
     * BSSID-filtered CSI stream; direct ESP-NOW mode needs it. */
#if !CONFIG_RESCUE_ROUTER_MODE
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
#endif

    /* Same configuration as Espressif's csi_recv examples.  LLTF-only keeps
     * the UART load sane at 100 Hz; manual scaling keeps amplitudes
     * comparable from packet to packet. */
    wifi_csi_config_t csi_config = {
        .lltf_en = true,
        .htltf_en = false,
        .stbc_htltf2_en = false,
        .ltf_merge_en = true,
        .channel_filter_en = true,
        .manu_scale = true,
        .shift = true,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

#if CONFIG_RESCUE_ROUTER_MODE

#define WIFI_GOT_IP_BIT BIT0

static EventGroupHandle_t s_wifi_events;
static volatile uint32_t s_gateway_ip;

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        printf("#WIFI,connecting,ssid=%s\n", CONFIG_RESCUE_WIFI_SSID);
        fflush(stdout);
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        const wifi_event_sta_connected_t *event = data;
        memcpy(s_expected_sender, event->bssid, sizeof(s_expected_sender));
        s_gain_reset_requested = true;
        printf("#WIFI,connected,channel=%d,bssid=" MACSTR "\n", (int)event->channel, MAC2STR(event->bssid));
        fflush(stdout);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = data;
        memset(s_expected_sender, 0, sizeof(s_expected_sender));
        /* Reason 201 = AP not found (wrong SSID or 5 GHz only), 15/204 =
         * wrong password.  The backend translates these for the dashboard. */
        printf("#WIFI,disconnected,reason=%d\n", (int)event->reason);
        fflush(stdout);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = data;
        s_gateway_ip = event->ip_info.gw.addr;
        printf("#WIFI,got_ip,ip=" IPSTR ",gw=" IPSTR "\n", IP2STR(&event->ip_info.ip), IP2STR(&event->ip_info.gw));
        fflush(stdout);
        xEventGroupSetBits(s_wifi_events, WIFI_GOT_IP_BIT);
    }
}

/* This is the official csi_recv_router ping method, restarted whenever the
 * station re-associates and the gateway address changes. */
static void router_link_task(void *arg)
{
    esp_ping_handle_t ping = NULL;
    uint32_t ping_target = 0;

    while (true) {
        xEventGroupWaitBits(s_wifi_events, WIFI_GOT_IP_BIT, pdTRUE, pdFALSE, portMAX_DELAY);
        uint32_t gateway = s_gateway_ip;
        if (ping != NULL && gateway == ping_target) {
            continue;
        }
        if (ping != NULL) {
            esp_ping_stop(ping);
            esp_ping_delete_session(ping);
            ping = NULL;
        }

        esp_ping_config_t config = ESP_PING_DEFAULT_CONFIG();
        config.count = 0; /* run until reset */
        config.interval_ms = 1000 / CONFIG_RESCUE_PING_HZ;
        config.task_stack_size = 3072;
        config.data_size = 1;
        config.target_addr.type = ESP_IPADDR_TYPE_V4;
        config.target_addr.u_addr.ip4.addr = gateway;

        esp_ping_callbacks_t callbacks = {0};
        esp_ip4_addr_t shown = {.addr = gateway};
        if (esp_ping_new_session(&config, &callbacks, &ping) == ESP_OK && esp_ping_start(ping) == ESP_OK) {
            ping_target = gateway;
            printf("#PING,gateway=" IPSTR ",hz=%d\n", IP2STR(&shown), CONFIG_RESCUE_PING_HZ);
        } else {
            ping = NULL;
            printf("#PING,error,gateway=" IPSTR "\n", IP2STR(&shown));
        }
        fflush(stdout);
    }
}

static void init_router_mode(void)
{
    s_wifi_events = xEventGroupCreate();
    configASSERT(s_wifi_events != NULL);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    configASSERT(esp_netif_create_default_wifi_sta() != NULL);

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = CONFIG_RESCUE_WIFI_SSID,
            .password = CONFIG_RESCUE_WIFI_PASSWORD,
            .scan_method = WIFI_ALL_CHANNEL_SCAN,
            .sort_method = WIFI_CONNECT_AP_BY_SIGNAL,
            /* Accept open, WPA2 and WPA3 hotspots alike. */
            .threshold.authmode = WIFI_AUTH_OPEN,
            .sae_pwe_h2e = WPA3_SAE_PWE_BOTH,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
#if CONFIG_RESCUE_FORCE_HT20
    /* Advertise 20 MHz only, so the AP never mixes 64- and 128-subcarrier
     * packets into one capture (the old backend reset its 30 s window on
     * every width change). */
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20));
#endif
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* Default STA modem sleep makes a 10 ms router-ping cadence irregular. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    wifi_csi_init();
    xTaskCreate(router_link_task, "router_link", 4096, NULL, 4, NULL);
}

#else

/* Direct mode follows esp-csi's csi_recv setup and works with stock csi_send. */
static void init_direct_espnow(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));

    esp_now_peer_info_t peer = {
        .channel = CONFIG_RESCUE_DIRECT_CHANNEL,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));

    esp_now_rate_config_t rate = {
        .phymode = WIFI_PHY_MODE_HT40,
        .rate = WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false,
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate));
}

static void init_direct_mode(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_create_default_wifi_sta() == NULL ? ESP_FAIL : ESP_OK);

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, DIRECT_RX_MAC));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT40));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_RESCUE_DIRECT_CHANNEL, WIFI_SECOND_CHAN_BELOW));

    memcpy(s_expected_sender, DIRECT_TX_MAC, sizeof(s_expected_sender));
    s_gain_reset_requested = true;
    init_direct_espnow();
    wifi_csi_init();
    printf("#DIRECT,channel=%d,sender=" MACSTR "\n", CONFIG_RESCUE_DIRECT_CHANNEL, MAC2STR(DIRECT_TX_MAC));
    fflush(stdout);
}
#endif

void app_main(void)
{
    init_console_io();
    init_nvs();
    init_alert_gpio();
    s_last_command_us = esp_timer_get_time();

    s_csi_queue = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(csi_frame_t));
    configASSERT(s_csi_queue != NULL);

#if CONFIG_RESCUE_ROUTER_MODE
    printf("#RESCUESENSE,fw=" FIRMWARE_VERSION ",mode=router,ping_hz=%d,gain=" GAIN_MODE_NAME ",format=CSI,timestamp_us,rssi,amplitudes\n",
           CONFIG_RESCUE_PING_HZ);
#else
    printf("#RESCUESENSE,fw=" FIRMWARE_VERSION ",mode=direct,channel=%d,gain=" GAIN_MODE_NAME ",format=CSI,timestamp_us,rssi,amplitudes\n",
           CONFIG_RESCUE_DIRECT_CHANNEL);
#endif
    fflush(stdout);

    xTaskCreate(csi_print_task, "csi_uart", 6144, NULL, 5, NULL);
    xTaskCreate(alert_task, "alert", 2560, NULL, 4, NULL);
    xTaskCreate(serial_command_task, "serial_cmd", 3072, NULL, 3, NULL);

#if CONFIG_RESCUE_ROUTER_MODE
    init_router_mode();
#else
    init_direct_mode();
#endif
    ESP_LOGI(TAG, "RescueSense receiver running");
}
