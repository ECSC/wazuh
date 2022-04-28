/* Copyright (C) 2015-2021, Wazuh Inc.
 * All rights reserved.
 *
 * This program is free software; you can redistribute it
 * and/or modify it under the terms of the GNU General Public
 * License (version 2) as published by the FSF - Free Software
 * Foundation.
 */

#include "opBuilderCondition.hpp"

#include <stdexcept>
#include <string>

#include "builders/opBuilderConditionReference.hpp"
#include "builders/opBuilderConditionValue.hpp"
#include "registry.hpp"
#include "syntax.hpp"

#include <logging/logging.hpp>

namespace builder::internals::builders
{

types::Lifter opBuilderCondition(const types::DocumentValue& def,
                                 types::TracerFn tr)
{
    // Check that input is as expected and throw exception otherwise
    if (!def.IsObject())
    {
        auto msg =
            fmt::format("Expexted type 'Object' but got [{}]", def.GetType());
        WAZUH_LOG_ERROR("{}", msg);
        throw std::invalid_argument(std::move(msg));
    }
    if (def.GetObject().MemberCount() != 1)
    {
        auto msg = fmt::format("Expected single key but got: [{}]",
                               def.GetObject().MemberCount());
        WAZUH_LOG_ERROR("{}", msg);
        throw std::invalid_argument(std::move(msg));
    }

    // Call apropiate builder depending on value
    auto v = def.MemberBegin();
    if (v->value.IsString())
    {
        std::string vStr = v->value.GetString();
        switch (vStr[0])
        {
            case syntax::FUNCTION_HELPER_ANCHOR:
                return Registry::getBuilder(
                    "helper." + vStr.substr(1, vStr.find("/") - 1))(def, tr);
                break;
            case syntax::REFERENCE_ANCHOR:
                return opBuilderConditionReference(def, tr);
                break;
            default: return opBuilderConditionValue(def, tr);
        }
    }
    else if (v->value.IsArray())
    {
        //TODO there isn't any "condition.array" function in the register
        return Registry::getBuilder("condition.array")(def, tr);
    }
    else if (v->value.IsObject())
    {
        //TODO there isn't any "condition.object" function in the register
        return Registry::getBuilder("condition.object")(def, tr);
    }
    else
    {
        return opBuilderConditionValue(def, tr);
    }
}

} // namespace builder::internals::builders
