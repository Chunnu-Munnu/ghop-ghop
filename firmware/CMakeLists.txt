# Derived from Espressif's esp-csi/examples/get-started/csi_recv_router.
# ESP-IDF v5.5.x. Wi-Fi is handled in main/, so protocol_examples_common is
# no longer needed. Note: ESP-IDF cannot build from a path containing spaces;
# tools/build-flash.ps1 copies this folder to C:\esp\rescuesense-fw first.
cmake_minimum_required(VERSION 3.16)
add_compile_options(-fdiagnostics-color=always)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rescue_sense_csi)
