Vagrant.configure("2") do |config|

    # Vagrant box to build off of.
    config.vm.box = "ubuntu/bionic64"
    config.disksize.size = '20GB'

    # Forward ports
    config.vm.network :forwarded_port, guest: 22, host: 2226, id: "ssh"
    config.vm.network :forwarded_port, guest: 8000, host: 8000
    config.vm.network :forwarded_port, guest: 80, host: 8001

    # Allocate resources
    config.vm.provider :virtualbox do |vb|
        vb.customize ["modifyvm", :id, "--ioapic", "on"]
        vb.customize ["modifyvm", :id, "--memory", "4096"]
        vb.customize ["modifyvm", :id, "--cpus", "4"]
    end

    # Set up a shared directory
    config.vm.synced_folder "shared", "/protwis/", :owner => "vagrant"

    # copy puppet scripts to VM
    config.vm.provision "file", source: "protwis_puppet_modules", destination: "/protwis/conf/protwis_puppet_modules"
    config.vm.provision "shell", inline: "sudo apt-get update && sudo apt-get install -y puppet"

    # Enable the Puppet provisioner
    config.vm.provision :puppet do |puppet|
        puppet.manifests_path = "manifests"
        puppet.manifest_file = "default.pp"
        puppet.module_path = "protwis_puppet_modules"
    end

end
