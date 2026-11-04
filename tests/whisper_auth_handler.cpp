#include "auth.hpp"

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <string>

using httplib::Server;

static Server::HandlerResponse check(
    const Server::HandlerWithResponse & handler,
    const std::string & method,
    const std::string & path,
    const std::string & authorization,
    int expected_status
) {
    httplib::Request request;
    httplib::Response response;
    request.method = method;
    request.path = path;
    if (!authorization.empty()) {
        request.headers.emplace("Authorization", authorization);
    }
    const auto result = handler(request, response);
    assert(response.status == expected_status);
    if (expected_status == 401) {
        assert(response.get_header_value("WWW-Authenticate") == "Bearer");
        assert(response.body.find("test-private-token") == std::string::npos);
    }
    return result;
}

int main() {
    const std::string token = "test-private-token";
    const auto handler = whisper_server_auth::make_pre_routing_handler(
        token, "/private"
    );

    assert(check(handler, "POST", "/private/inference", "", 401)
        == Server::HandlerResponse::Handled);
    assert(check(handler, "POST", "/private/load", "", 401)
        == Server::HandlerResponse::Handled);
    assert(check(handler, "GET", "/private/health", "", -1)
        == Server::HandlerResponse::Unhandled);
    assert(check(handler, "GET", "/health", "", 401)
        == Server::HandlerResponse::Handled);
    assert(check(handler, "GET", "/private/health/", "", 401)
        == Server::HandlerResponse::Handled);
    assert(check(handler, "HEAD", "/private/health", "", 401)
        == Server::HandlerResponse::Handled);
    assert(check(handler, "POST", "/private/inference", "Bearer wrong", 401)
        == Server::HandlerResponse::Handled);
    assert(check(
        handler, "POST", "/private/inference", "Bearer " + token, -1
    ) == Server::HandlerResponse::Unhandled);
    assert(check(
        handler, "POST", "/private/load", "Bearer " + token, -1
    ) == Server::HandlerResponse::Unhandled);
    assert(whisper_server_auth::is_loopback_bind_host("127.0.0.1"));
    assert(whisper_server_auth::is_loopback_bind_host("::1"));
    assert(whisper_server_auth::is_loopback_bind_host("localhost"));
    assert(!whisper_server_auth::is_loopback_bind_host("0.0.0.0"));
    assert(!whisper_server_auth::is_loopback_bind_host("192.0.2.10"));

    const auto nonce = std::chrono::steady_clock::now()
        .time_since_epoch().count();
    const auto key_path = std::filesystem::temp_directory_path()
        / ("whisper-auth-key-" + std::to_string(nonce));
    std::string loaded_key;
    {
        std::ofstream output(key_path, std::ios::binary);
        output << token << "\n";
    }
    assert(whisper_server_auth::load_api_key_file(key_path.string(), loaded_key));
    assert(loaded_key == token);
    {
        std::ofstream output(key_path, std::ios::binary | std::ios::trunc);
        output << "first-line\nsecond-line\n";
    }
    assert(!whisper_server_auth::load_api_key_file(key_path.string(), loaded_key));
    {
        std::ofstream output(key_path, std::ios::binary | std::ios::trunc);
    }
    assert(!whisper_server_auth::load_api_key_file(key_path.string(), loaded_key));
    {
        std::ofstream output(key_path, std::ios::binary | std::ios::trunc);
        output << "token with whitespace\n";
    }
    assert(!whisper_server_auth::load_api_key_file(key_path.string(), loaded_key));
    {
        std::ofstream output(key_path, std::ios::binary | std::ios::trunc);
        output << std::string(4097, 'a');
    }
    assert(!whisper_server_auth::load_api_key_file(key_path.string(), loaded_key));
    {
        std::ofstream output(key_path, std::ios::binary | std::ios::trunc);
        output << std::string(8192, 'a');
    }
    assert(!whisper_server_auth::load_api_key_file(key_path.string(), loaded_key));
    assert(!whisper_server_auth::load_api_key_file(
        (key_path.string() + ".missing"), loaded_key
    ));
    std::filesystem::remove(key_path);
}
