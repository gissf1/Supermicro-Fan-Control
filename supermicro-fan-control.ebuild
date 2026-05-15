# Distributed under the terms of the GNU General Public License v3

EAPI=8

DESCRIPTION="Intelligent fan control script for Supermicro motherboards"
HOMEPAGE="https://github.com/gissf1/Supermicro-Fan-Control"
SRC_URI="https://github.com/gissf1/Supermicro-Fan-Control/releases/download/v${PV}/supermicro-fan-control.deb -> ${P}.deb"

LICENSE="GPL-3"
SLOT="0"
KEYWORDS="amd64 x86"

RDEPEND="dev-lang/python sys-apps/ipmitool"

src_install() {
	dobin usr/bin/fan-control
	insinto /etc
	doins etc/fan-control.ini
	systemd_dounit lib/systemd/system/fan-control.service
}
