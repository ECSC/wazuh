/* Copyright (C) 2015-2022, Wazuh Inc.
 * All rights reserved.
 *
 * This program is free software; you can redistribute it
 * and/or modify it under the terms of the GNU General Public
 * License (version 2) as published by the FSF - Free Software
 * Foundation.
 */

#include "stageParse.hpp"

#include <any>
#include <stdexcept>
#include <string>
#include <typeindex>
#include <typeinfo>
#include <vector>
// TODO test
#include <fstream>

#include <fmt/format.h>

#include "builders/combinatorBuilderBroadcast.hpp"
#include "registry.hpp"
#include <hlp/hlp.hpp>
#include <logging/logging.hpp>

namespace builder::internals::builders
{
static bool
any2Json(std::any const& anyVal, std::string const& path, json::Document* doc)
{
    auto& type = anyVal.type();
    if (type == typeid(void))
    {
        doc->set(path, {nullptr, doc->getAllocator()});
    }
    else if (type == typeid(long))
    {
        json::Value val;
        val.SetInt64(std::any_cast<long>(anyVal));
        doc->set(path, val);
    }
    else if (type == typeid(int))
    {
        json::Value val;
        val.SetInt(std::any_cast<int>(anyVal));
        doc->set(path, val);
    }
    else if (type == typeid(unsigned))
    {
        json::Value val;
        val.SetUint(std::any_cast<unsigned>(anyVal));
        doc->set(path, val);
    }
    else if (type == typeid(float))
    {
        json::Value val;
        val.SetFloat(std::any_cast<float>(anyVal));
        doc->set(path, val);
    }
    else if (type == typeid(double))
    {
        json::Value val;
        val.SetDouble(std::any_cast<double>(anyVal));
        doc->set(path, val);
    }
    else if (type == typeid(std::string))
    {
        const auto& s = std::any_cast<std::string>(anyVal);
        doc->set(path, {s.c_str(), doc->getAllocator()});
    }
    else if (type == typeid(hlp::JsonString))
    {
        const auto& s = std::any_cast<hlp::JsonString>(anyVal);
        rapidjson::Document d(&doc->getAllocator());
        d.Parse<rapidjson::kParseStopWhenDoneFlag>(s.jsonString.data());
        doc->set(path, d);
    }
    else
    {
        // ASSERT
        return false;
    }
    return true;
}

types::Lifter stageBuilderParse(const types::DocumentValue& def,
                                types::TracerFn tr)
{
    // Assert value is as expected
    if (!def.IsObject())
    {
        std::string msg = fmt::format(
            "[Stage parse] builder, expected array but got {}", def.GetType());
        WAZUH_LOG_ERROR("{}", msg);
        throw std::invalid_argument(msg);
    }

    auto parseObj = def.GetObj();

    if (!parseObj["logql"].IsArray())
    {
        // TODO ERROR
        WAZUH_LOG_ERROR("Parse stage is ill formed.");
        throw std::invalid_argument(
            "[Stage parse]Config format error. Check the parser section.");
    }

    auto const& logqlArr = parseObj["logql"];
    if (logqlArr.Empty())
    {
        // TODO error
        WAZUH_LOG_ERROR("No logQl expressions found.");
        throw std::invalid_argument(
            "[Stage parse]Must have some expressions configured.");
    }

    std::vector<types::Lifter> parsers;
    for (auto const& item : logqlArr.GetArray())
    {
        if (!item.IsObject())
        {
            WAZUH_LOG_ERROR("LogQl object is badly formatted.");
            throw std::invalid_argument(
                "[Stage parse]Bad format trying to get parse expression ");
        }

        auto logql = item.GetObj().begin();

        ParserFn parseOp;
        try
        {
            parseOp = hlp::getParserOp(logql->value.GetString());
        }
        catch (std::runtime_error& e)
        {
            const char* msg =
                "Stage [parse] builder encountered exception parsing logQl "
                "expr";
            WAZUH_LOG_ERROR("{} From exception: {}", msg, e.what());
            std::throw_with_nested(std::runtime_error(msg));
        }

        auto newOp = [name = std::string {logql->name.GetString()},
                      parserOp = std::move(parseOp)](types::Observable o)
        {
            return o.map(
                [name = std::move(name),
                 parserOp = std::move(parserOp)](types::Event e)
                {
                    // TODO handle item not existing in event
                    auto jsonName = json::formatJsonPath(name);
                    const auto& ev = e->get(jsonName);
                    if (!ev.IsString())
                    {
                        // TODO error
                        return e;
                    }

                    ParseResult result;
                    auto ok = parserOp(ev.GetString(), result);
                    if (!ok)
                    {
                        // TODO error
                        return e;
                    }

                    for (auto const& val : result)
                    {
                        auto resultPath =
                            json::formatJsonPath(val.first.c_str());
                        if (!any2Json(val.second, resultPath, e.get()))
                        {
                            // ERROR
                            return e;
                        }
                    }

                    return e;
                });
        };

        parsers.emplace_back(newOp);
    }

    try
    {
        auto check = combinatorBuilderBroadcast(parsers);
        return check;
    }
    catch (std::exception& e)
    {
        const char* msg = "Stage [parse] builder encountered exception "
                          "chaining all mappings.";
        WAZUH_LOG_ERROR("{} From exception: {}", msg, e.what());
        std::throw_with_nested(std::runtime_error(msg));
    }
}
} // namespace builder::internals::builders
