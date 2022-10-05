#ifndef _API_WAZUH_RESPONSE_HPP
#define _API_WAZUH_RESPONSE_HPP

#include <json/json.hpp>
#include <logging/logging.hpp>

namespace api
{

/**
 * @brief A standard protocol for internal communication between Wazuh components
 *
 * https://github.com/wazuh/wazuh/issues/5934
 */
class WazuhResponse
{
private:
    // Mandatory fields for all responses
    int m_error;                          ///< Error code
    json::Json m_data;                    ///< Data
    std::optional<std::string> m_message; ///< Optional message

public:
    // TODO Delete explicit when json constructor does not throw exceptions
    /**
     * @brief  Construct a new Wazuh Response object
     *
     * @param data Data to be sent, it can be a json object or a string
     * @param error Error code (0 if no error)
     * @param message Optional message
     */
    explicit WazuhResponse(const json::Json& data,
                           int error,
                           std::string_view message = "") noexcept
        : m_data(data)
        , m_error(error)
    {
        m_message = message.empty() ? std::nullopt : std::optional<std::string> {message};
    }

    /**
     * @brief Return data object of the response
     *
     * @return data object
     */
    const json::Json& data() const { return m_data; }

    /**
     * @brief Return error code of the response
     *
     * @return error code
     */
    int error() const { return m_error; }

    /**
     * @brief Return message of the response if exists
     *
     * @return message of the response if exists
     */
    const std::optional<std::string>& message() const { return m_message; }

    /**
     * @brief Set data object of the response, overwriting the previous one
     *
     * @param data object
     */
    void data(const json::Json& data) { m_data = data; }

    /**
     * @brief Set error code of the response, overwriting the previous one
     *
     * @param error code
     */
    void error(int error) { m_error = error; }

    /**
     * @brief Set message of the response, overwriting the previous one if exists
     *
     * @param message of the response
     */
    void message(const std::string& message) { m_message = message; }

    /**
     * @brief Conver the response to a string according to the protocol
     *
     * @return response as a string
     */
    std::string toString() const
    {
        if (m_message.has_value())
        {
            return fmt::format("{{\"data\":{},\"error\":{},\"message\":\"{}\"}}",
                               m_data.str(),
                               m_error,
                               m_message.value());
        }
        return fmt::format("{{\"data\":{},\"error\":{}}}", m_data.str(), m_error);
    }

    /**
     * @brief Validate the response
     *
     * A response is valid if the data is a json object or a array
     * @return true
     * @return false
     */
    bool isValid() const { return !(!m_data.isObject() && !m_data.isArray()); }
};

} // namespace api

#endif // _API_WAZUH_RESPONSE_HPP
