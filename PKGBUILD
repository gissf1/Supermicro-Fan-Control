# Maintainer: Brian G <gissf1@gmail.com>
pkgname=supermicro-fan-control
pkgver=PACKAGE_VERSION
pkgrel=1
pkgdesc="Intelligent fan control script for Supermicro motherboards"
arch=('i686' 'x86_64')
url="https://github.com/gissf1/Supermicro-Fan-Control"
license=('GPL3')
depends=('python' 'ipmitool')
backup=('etc/fan-control.ini')
source=("${url}/releases/download/v${pkgver}/supermicro-fan-control.deb")
sha256sums=('SKIP')

package() {
	cd "${srcdir}"
	install -Dm755 "usr/bin/fan-control" "${pkgdir}/usr/bin/fan-control"
	install -Dm644 "etc/fan-control.ini" "${pkgdir}/etc/fan-control.ini"
	install -Dm644 "lib/systemd/system/fan-control.service" "${pkgdir}/usr/lib/systemd/system/fan-control.service"
}
