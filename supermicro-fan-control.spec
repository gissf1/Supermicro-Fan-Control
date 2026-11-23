Name:           supermicro-fan-control
Version:        %{?pkg_version}%{!?pkg_version:0.0.0}
Release:        1%{?dist}
Summary:        Intelligent fan control for Supermicro motherboards
License:        GPL-3.0-or-later
URL:            https://github.com/gissf1/Supermicro-Fan-Control
BuildArch:      noarch
Source0:        fan-control.py
Source1:        fan-control.ini
Source2:        fan-control.service
Requires:       python3
Requires:       ipmitool
# Store the UTC release date as a queryable RPM capability.
Provides:       supermicro-fan-control-release-date = %{?release_date}%{!?release_date:19700101}

%description
A Python service that monitors configured temperatures and controls fan
zones on supported Supermicro motherboards through IPMI.

%install
install -Dpm 0755 %{SOURCE0} %{buildroot}%{_bindir}/fan-control
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_sysconfdir}/fan-control.ini
install -Dpm 0644 %{SOURCE2} %{buildroot}%{_unitdir}/fan-control.service

%files
%{_bindir}/fan-control
%config(noreplace) %{_sysconfdir}/fan-control.ini
%{_unitdir}/fan-control.service
