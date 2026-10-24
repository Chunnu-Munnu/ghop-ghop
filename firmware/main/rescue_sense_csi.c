/*
 * RescueSense CSI receiver
 *
 * Derived from Espressif's official esp-csi examples:
 *   examples/get-started/csi_recv_router/main/app_main.c
 *   examples/get-started/csi_recv/main/app_main.c
 * SPDX-License-Identifier: CC0-1.0
 *
 * Router mode is the fast one-board path.  It joins an AP and requests a
 * gateway ping at 100 Hz; CSI is extracted from the AP's replies.  Direct
 * mode is for Espressif's stock csi_send running on a second ESP32.
 */

#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_csi_gain_ctrl.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "lwip/ip4_addr.h"
#include "nvs_flash.h"
#include "ping/ping_sock.h"
#include "protocol_examples_common.h"

#define TAG "rescuesense"
#define CSI_BYTES_MAX 612
#define CSI_QUEUE_DEPTH 16
#define CSI_AMPLITUDES_MAX 128

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
    uint8_t bytes[CSI_BYTES_MAX];
} csi_frame_t;

static QueueHandle_t s_csi_queue;
static uint8_t s_expected_sender[6];
static volatile bool s_alert_active;
static volatile uint32_t s_dropped_frames;

static void init_nvs(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
}

static void alert_task(void *arg)
{
    bool flash = false;
    while (true) {
        if (s_alert_active) {
            flash = !flash;
            gpio_set_level(CONFIG_RESCUE_BUZZER_GPIO, flash ? 1 : 0);
            vTaskDelay(pdMS_TO_TICKS(125));
        } else {
            flash = false;
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

/* The backend writes ALERT,1 or ALERT,0 to this same USB/UART serial port. */
static void serial_command_task(void *arg)
{
    char line[32];
    while (true) {
        if (fgets(line, sizeof(line), stdin) == NULL) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }

        if (strncmp(line, "ALERT,1", 7) == 0) {
            s_alert_active = true;
            printf("#ALERT,1\n");
            fflush(stdout);
        } else if (strncmp(line, "ALERT,0", 7) == 0) {
            s_alert_active = false;
            printf("#ALERT,0\n");
            fflush(stdout);
        } else if (strncmp(line, "STATUS", 6) == 0) {
            printf("#STATUS,alert=%d,dropped=%" PRIu32 "\n", s_alert_active, s_dropped_frames);
            fflush(stdout);
        }
    }
}

/*
 * Keep this callback deliberately short: ESP-IDF runs it in the Wi-Fi task.
 * Magnitude conversion and UART I/O happen in csi_print_task instead.
 */
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (info == NULL || info->buf == NULL || info->len == 0) {
        return;
    }
    if (memcmp(info->mac, s_expected_sender, sizeof(s_expected_sender)) != 0) {
        return;
    }

    csi_frame_t frame = {
        .timestamp_us = (uint64_t)esp_timer_get_time(),
        .len = info->len > CSI_BYTES_MAX ? CSI_BYTES_MAX : info->len,
        .rssi = info->rx_ctrl.rssi,
        .first_word_invalid = info->first_word_invalid,
        .gain_compensation = 1.0f,
    };

    /* Preserves the gain-compensation step used by Espressif's CSI samples. */
    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc_gain, &fft_gain);
    esp_csi_gain_ctrl_get_gain_compensation(&frame.gain_compensation, agc_gain, fft_gain);

    memcpy(frame.bytes, info->buf, frame.len);
    if (xQueueSend(s_csi_queue, &frame, 0) != pdPASS) {
        ++s_dropped_frames;
    }
}

/* Compact wire protocol: CSI,timestamp_us,rssi,amp_sc0,amp_sc1,... */
static void csi_print_task(void *arg)
{
    csi_frame_t frame;
    printf("#RESCUESENSE,CSI,timestamp_us,rssi,subcarrier_amplitudes\n");
    fflush(stdout);

    while (true) {
        if (xQueueReceive(s_csi_queue, &frame, portMAX_DELAY) != pdPASS) {
            continue;
        }

        /*
         * Preserve a constant vector width.  ESP32-S3 may mark the first
         * four bytes invalid; emit zeros for those two raw bins instead of
         * shortening this CSV row.  The host's fixed carrier mask removes
         * them before PCA, so alternating first_word_invalid packets never
         * reset the 30-second processing window.
         */
        uint16_t sample_count = frame.len / 2;
        if (sample_count > CSI_AMPLITUDES_MAX) {
            sample_count = CSI_AMPLITUDES_MAX;
        }

        printf("CSI,%" PRIu64 ",%d", frame.timestamp_us, frame.rssi);
        for (uint16_t sc = 0; sc < sample_count; ++sc) {
            if (frame.first_word_invalid && sc < 2) {
                printf(",0");
                continue;
            }
            uint16_t byte = 2 * sc;
            int8_t imag = (int8_t)frame.bytes[byte];
            int8_t real = (int8_t)frame.bytes[byte + 1];
            float magnitude = sqrtf((float)real * real + (float)imag * imag)
                            * frame.gain_compensation;
            int output = (int)lroundf(magnitude);
            printf(",%d", output < 0 ? 0 : output);
        }
        printf("\n");
        fflush(stdout);
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

    /* LLTF-only keeps UART throughput reliable at 100 Hz for router and TX modes. */
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

/* This is the official csi_recv_router ping method, kept at 100 Hz by default. */
static void start_router_ping(void)
{
    esp_netif_t *sta_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t local_ip = {0};
    ESP_ERROR_CHECK(esp_netif_get_ip_info(sta_netif, &local_ip));

    esp_ping_config_t config = ESP_PING_DEFAULT_CONFIG();
    config.count = 0;                   /* run until reset */
    config.interval_ms = 1000 / CONFIG_RESCUE_PING_HZ;
    config.task_stack_size = 3072;
    config.data_size = 1;
    config.target_addr.type = ESP_IPADDR_TYPE_V4;
    config.target_addr.u_addr.ip4.addr = ip4_addr_get_u32(&local_ip.gw);

    static esp_ping_handle_t ping_handle;
    esp_ping_callbacks_t callbacks = {0};
    ESP_ERROR_CHECK(esp_ping_new_session(&config, &callbacks, &ping_handle));
    ESP_ERROR_CHECK(esp_ping_start(ping_handle));
    ESP_LOGI(TAG, "router CSI: gateway=" IPSTR ", target=%d Hz", IP2STR(&local_ip.gw), CONFIG_RESCUE_PING_HZ);
}

static void init_router_mode(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(example_connect());
    /* Default STA modem sleep makes a 10 ms router-ping cadence irregular. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    wifi_ap_record_t ap = {0};
    ESP_ERROR_CHECK(esp_wifi_sta_get_ap_info(&ap));
    memcpy(s_expected_sender, ap.bssid, sizeof(s_expected_sender));
    ESP_LOGI(TAG, "router BSSID=" MACSTR, MAC2STR(s_expected_sender));

    wifi_csi_init();
    start_router_ping();
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
    init_direct_espnow();
    wifi_csi_init();
    ESP_LOGI(TAG, "direct CSI: channel=%d, sender=" MACSTR, CONFIG_RESCUE_DIRECT_CHANNEL, MAC2STR(DIRECT_TX_MAC));
}
#endif

void app_main(void)
{
    init_nvs();
    init_alert_gpio();
    s_csi_queue = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(csi_frame_t));
    configASSERT(s_csi_queue != NULL);

    xTaskCreate(csi_print_task, "csi_uart", 6144, NULL, 5, NULL);
    xTaskCreate(alert_task, "alert", 2048, NULL, 4, NULL);
    xTaskCreate(serial_command_task, "serial_cmd", 3072, NULL, 3, NULL);

#if CONFIG_RESCUE_ROUTER_MODE
    init_router_mode();
#else
    init_direct_mode();
#endif
}
