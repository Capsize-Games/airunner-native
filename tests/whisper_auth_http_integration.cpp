#include "auth.hpp"

#include <atomic>
#include <cassert>
#include <string>
#include <thread>

int main() {
    const std::string token = "test-private-token";
    std::atomic<int> inference_route_hits{0};
    std::atomic<int> load_route_hits{0};
    httplib::Server server;
    server.set_pre_routing_handler(
        whisper_server_auth::make_pre_routing_handler(token, "")
    );
    server.set_error_handler(whisper_server_auth::handle_server_error);
    server.Get("/health", [](const httplib::Request &, httplib::Response & response) {
        response.set_content("{\"status\":\"ok\"}", "application/json");
    });
    server.Post("/inference", [&](const httplib::Request &, httplib::Response & response) {
        ++inference_route_hits;
        response.set_content("inference route reached", "text/plain");
    });
    server.Post("/load", [&](const httplib::Request &, httplib::Response & response) {
        ++load_route_hits;
        response.set_content("load route reached", "text/plain");
    });

    const int port = server.bind_to_any_port("127.0.0.1");
    assert(port > 0);
    std::thread listener([&server]() { assert(server.listen_after_bind()); });
    server.wait_until_ready();

    httplib::Client client("127.0.0.1", port);
    const auto unauthorized_inference = client.Post("/inference");
    assert(unauthorized_inference);
    assert(unauthorized_inference->status == 401);
    assert(unauthorized_inference->get_header_value("WWW-Authenticate") == "Bearer");
    assert(unauthorized_inference->body == "{\"error\":\"unauthorized\"}");
    assert(unauthorized_inference->body.find(token) == std::string::npos);
    assert(inference_route_hits == 0);

    const auto unauthorized_load = client.Post("/load");
    assert(unauthorized_load);
    assert(unauthorized_load->status == 401);
    assert(unauthorized_load->get_header_value("WWW-Authenticate") == "Bearer");
    assert(unauthorized_load->body == "{\"error\":\"unauthorized\"}");
    assert(load_route_hits == 0);

    const auto health = client.Get("/health");
    assert(health && health->status == 200);

    const auto authorized_headers = httplib::Headers{
        {"Authorization", "Bearer " + token}
    };
    const auto authorized_inference = client.Post(
        "/inference", authorized_headers, "", "application/octet-stream"
    );
    assert(authorized_inference && authorized_inference->status == 200);
    assert(inference_route_hits == 1);
    const auto authorized_load = client.Post(
        "/load", authorized_headers, "", "application/octet-stream"
    );
    assert(authorized_load && authorized_load->status == 200);
    assert(load_route_hits == 1);

    server.stop();
    listener.join();
}
