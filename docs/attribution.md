# Attribution

The generator is under the [Apache License 2.0](../LICENSE). The unchanged
project license is also in [LICENSES/Apache-2.0.txt](../LICENSES/Apache-2.0.txt).
The content that NocLoom supplies in the output files is under the
[NocLoom Output Permission 1.0](../LICENSES/LicenseRef-NocLoom-Output-1.0.txt).
The permission does not cover user material, third party material or the
generator itself.

[NOTICE](../NOTICE) indexes the attribution materials. The license texts below
retain their supplied notices. The nlohmann_json text also retains copyright
notices from its bundled helper headers.

| Dependency | Accepted version | License text | Use |
| --- | --- | --- | --- |
| nlohmann_json | 3.11.3 or newer | [MIT](../third_party/licenses/nlohmann_json.txt) | JSON code compiled into Core and the CLI |
| yaml-cpp | 0.8.0 or newer | [MIT](../third_party/licenses/yaml-cpp.txt) | Parser linked by Config |
| CLI11 | 2.4.1 or newer | [BSD 3 Clause](../third_party/licenses/CLI11.txt) | Argument parsing compiled into the CLI |
| Catch2 | 3.4.0 or newer within version 3 | [Boost Software License 1.0](../third_party/licenses/Catch2.txt) | Test executables only |

The source package contains NocLoom sources and these license texts. It obtains
dependency code through CMake packages. The build creates static NocLoom
libraries. It does not create shared NocLoom libraries.

| Distributed content | Accompanying materials |
| --- | --- |
| Core static library | NocLoom LICENSE, NOTICE and nlohmann_json license |
| Config and IO static libraries | Core materials and yaml-cpp license |
| CLI executable | Config materials and CLI11 license |
| A separately supplied static or shared yaml-cpp library | Its own license and notices |
| Test executables | Catch2 license and the materials for linked NocLoom components |

Installation places LICENSE, LICENSES, NOTICE, this page, the Catch2 license
text and the applicable dependency license texts under `share/nocloom` by
default. The yaml-cpp library comes from the dependency package. Include its
materials when distributing that library. For other dependency versions, retain
the license and notices supplied with those versions.

## Protocol names

Arm® AMBA® protocol names identify the implemented interfaces. Arm and AMBA are
registered trademarks of Arm Limited (or its subsidiaries or affiliates) in the
US and/or elsewhere. NocLoom does not claim Arm certification or endorsement.
See [Arm trademark guidance](https://www.arm.com/company/policies/trademarks)
and
[AMBA trademark guidance](https://www.arm.com/company/policies/trademarks/arm-trademark-list/amba-trademark).

## Logo

The letters in the NocLoom logo are outlines of the Roboto typeface, version 2,
which is licensed under the Apache License 2.0. The typeface carries the notice
"Copyright 2011 Google Inc. All Rights Reserved."
