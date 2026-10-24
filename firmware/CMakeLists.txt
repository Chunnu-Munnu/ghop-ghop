# Derived from Espressif's esp-csi/examples/get-started/csi_recv_router.
# ESP-IDF v5.5.6; protocol_examples_common provides example_connect().
cmake_minimum_required(VERSION 3.16)
add_compile_options(-fdiagnostics-color=always)

set(EXTRA_COMPONENT_DIRS $ENV{IDF_PATH}/examples/common_components/protocol_examples_common)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(rescue_sense_csi)
