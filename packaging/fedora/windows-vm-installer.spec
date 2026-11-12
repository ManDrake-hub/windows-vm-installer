# No release tags yet: Source0 points at the main branch snapshot (a moving
# target). For a tagged release, point Source0 at the tag tarball and update
# the %setup directory name below.
%global github_url https://github.com/ManDrake-hub/windows-vm-installer

Name:           windows-vm-installer
Version:        0.1.0
Release:        1%{?dist}
Summary:        Install Windows onto a physical disk from Linux, through a hidden virtual machine

License:        GPL-3.0-or-later
URL:            %{github_url}
# no release tags yet: build from the repository snapshot at this commit
Source0:        %{github_url}/archive/refs/heads/main.tar.gz

BuildArch:      noarch

BuildRequires:  python-rpm-macros
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3
BuildRequires:  python3-build
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils
BuildRequires:  appstream

Requires:       python3
Requires:       qemu-system-x86
Requires:       edk2-ovmf
Requires:       swtpm
Requires:       efibootmgr
Requires:       util-linux
Recommends:     python3-gobject
Recommends:     gtk4
Recommends:     libadwaita

%description
The Windows installer runs inside a hidden QEMU/KVM virtual machine that can
see only the target disk. When its first phase is done, you reboot into the
new disk and Windows finishes the setup on the real hardware: drivers,
account, activation.

No USB stick is needed, and the Linux disk is safe: the installer cannot see
it. Windows gets its own EFI system partition, the edition is chosen
explicitly, and the new system is added to the boot manager (Limine, GRUB,
rEFInd).

%prep
%setup -q -n windows-vm-installer-main

# %pyproject_wheel builds without isolation (no PyPI access): the PEP 517
# backend comes from python3-setuptools and python3-wheel in BuildRequires.
# Keep this comment outside %build: inside, it would be swallowed by the macro.
%build
%pyproject_wheel

%install
%pyproject_install
install -Dm644 "data/io.github.mandrake_hub.WindowsVmInstaller.desktop" \
  "$RPM_BUILD_ROOT/usr/share/applications/io.github.mandrake_hub.WindowsVmInstaller.desktop"
install -Dm644 "data/io.github.mandrake_hub.WindowsVmInstaller.metainfo.xml" \
  "$RPM_BUILD_ROOT/usr/share/metainfo/io.github.mandrake_hub.WindowsVmInstaller.metainfo.xml"
install -Dm644 "data/icons/hicolor/scalable/apps/io.github.mandrake_hub.WindowsVmInstaller.svg" \
  "$RPM_BUILD_ROOT/usr/share/icons/hicolor/scalable/apps/io.github.mandrake_hub.WindowsVmInstaller.svg"
install -Dm644 "data/icons/hicolor/256x256/apps/io.github.mandrake_hub.WindowsVmInstaller.png" \
  "$RPM_BUILD_ROOT/usr/share/icons/hicolor/256x256/apps/io.github.mandrake_hub.WindowsVmInstaller.png"

%check
PYTHONPATH=src python3 -m unittest discover -s tests
desktop-file-validate "$RPM_BUILD_ROOT/usr/share/applications/io.github.mandrake_hub.WindowsVmInstaller.desktop"
appstreamcli validate --no-net "$RPM_BUILD_ROOT/usr/share/metainfo/io.github.mandrake_hub.WindowsVmInstaller.metainfo.xml"

%files
%license LICENSE
%doc README.md
%{_bindir}/windows-vm-installer
%{_bindir}/windows-vm-installer-gui
%{python3_sitelib}/wvi
%{python3_sitelib}/windows_vm_installer-*.dist-info
%{_datadir}/applications/io.github.mandrake_hub.WindowsVmInstaller.desktop
%{_datadir}/metainfo/io.github.mandrake_hub.WindowsVmInstaller.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.mandrake_hub.WindowsVmInstaller.svg
%{_datadir}/icons/hicolor/256x256/apps/io.github.mandrake_hub.WindowsVmInstaller.png

%changelog
* Thu Nov 12 2026 Geraldino Mandragora <mandrake@wholetech.eu> - 0.1.0-1
- Initial packaging
