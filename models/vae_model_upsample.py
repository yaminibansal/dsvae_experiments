import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.autograd import Variable

def truncated_normal_(tensor, mean=0, std=0.02):
    size = tensor.shape
    tmp = tensor.new_empty(size + (4,)).normal_()
    valid = (tmp < 2) & (tmp > -2)
    ind = valid.max(-1, keepdim=True)[1]
    tensor.data.copy_(tmp.gather(-1, ind).squeeze(-1))
    tensor.data.mul_(std).add_(mean)
    
def weights_init(m):
    if isinstance(m, (nn.Conv2d, nn.ConvTranspose2d)):
        with torch.no_grad():
            truncated_normal_(m.weight.data, mean=0, std=0.02)
        nn.init.constant_(m.bias.data, 0.0)
    elif isinstance(m, nn.Linear):
        nn.init.normal_(m.weight.data, mean=0, std=0.02)
        nn.init.constant_(m.bias.data, 0.0)
        
class UPSAMPLE_VAE(nn.Module):
    def __init__(self, z_dim, input_shape, upsample_from_shape, device=None):
        super(UPSAMPLE_VAE, self).__init__()
        self.z_dim = z_dim
        self.input_shape = input_shape
        self.input_nc = input_shape[0]
        self.input_height = input_shape[1]
        self.input_width = input_shape[2]
        
        self.upsample_from_shape = upsample_from_shape
        self.upsample_from_nc = upsample_from_shape[0]
        self.upsample_from_height = upsample_from_shape[1]
        self.upsample_from_width = upsample_from_shape[2]
        
        if device is None or device == -1:
            self.cuda = False
            self.device = None
        else:
            self.device = device
            self.cuda = True
        
        #ENCODER
        self.e1 = nn.Conv2d(self.input_nc, 64, 4, stride=2, padding=1)  #[b,64,inh/2,inw/2]
        weights_init(self.e1)
        self.instance_norm_e1 = nn.InstanceNorm2d(num_features=64, affine=False)

        self.e2 = nn.Conv2d(64, 128, 4, stride=2, padding=1)  #[b,128,inh/4,inw/4]
        weights_init(self.e2)
        self.instance_norm_e2 = nn.InstanceNorm2d(num_features=128, affine=False)

        self.e3 = nn.Conv2d(128, 256, 4, stride=2, padding=1)  #[b,256,inh/8,inw/8]
        weights_init(self.e3)
        self.instance_norm_e3 = nn.InstanceNorm2d(num_features=256, affine=False)

        self.e4 = nn.Conv2d(256, 512, 4, stride=2, padding=1)  #[b,512,inh/16,inw/16]
        weights_init(self.e4)
        self.instance_norm_e4 = nn.InstanceNorm2d(num_features=512, affine=False)

        self.fc_mean = nn.Linear(512*int(self.input_height/16)*int(self.input_width/16), z_dim)
        weights_init(self.fc_mean)
        self.fc_var = nn.Linear(512*int(self.input_height/16)*int(self.input_width/16), z_dim)
        weights_init(self.fc_var)
        
        #DECODER
        self.fc2 = nn.Linear(z_dim, 512*int(self.upsample_from_height/16)*int(self.upsample_from_width/16))
        weights_init(self.fc2)
        self.instance_norm_d5 = nn.InstanceNorm2d(num_features=512, affine=False) 
        
        self.d5 = nn.ConvTranspose2d(512, 256, 4, stride=2, padding=1) #[b, 256,uph/8,upw/8]
        weights_init(self.d5)
        self.instance_norm_d6 = nn.InstanceNorm2d(num_features=256, affine=False) 
        
        self.d6 = nn.ConvTranspose2d(256, 128, 4, stride=2, padding=1) #[b, 128,uph/4,upw/4]
        weights_init(self.d6)
        self.instance_norm_d7 = nn.InstanceNorm2d(num_features=128, affine=False) 
        
        self.d7 = nn.ConvTranspose2d(128, 64, 4, stride=2, padding=1) #[b, 64,uph/2,upw/2]
        weights_init(self.d7)
        self.instance_norm_d8 = nn.InstanceNorm2d(num_features=64, affine=False) 
        
        self.d8 = nn.ConvTranspose2d(64, self.input_nc, 4, stride=2, padding=1) #[b, uph,upw]
        weights_init(self.d8)
        
        self.leakyrelu = nn.LeakyReLU(0.2)
        self.relu = nn.ReLU()
        self.sigmoid = nn.Sigmoid()

    def encode(self, x):
        h = self.leakyrelu(self.instance_norm_e1(self.e1(x)))   
        h = self.leakyrelu(self.instance_norm_e2(self.e2(h)))    
        h = self.leakyrelu(self.instance_norm_e3(self.e3(h)))     
        h = self.leakyrelu(self.instance_norm_e4(self.e4(h)))
        h = h.view(-1,512*int(self.input_height/16)*int(self.input_width/16))
        return self.fc_mean(h), F.softplus(self.fc_var(h))

    def reparametrize(self, mu, var):
        std = torch.sqrt(var)
        if self.cuda:
            eps = torch.FloatTensor(std.size()).normal_().to(self.device)
        else:
            eps = torch.FloatTensor(std.size()).normal_()
        eps = Variable(eps)
        return eps.mul(std).add_(mu) 

    def decode(self, z):
        h = self.relu(self.fc2(z))
        h = h.reshape(-1, 512, int(self.upsample_from_height/16), int(self.upsample_from_width/16))
        h = self.relu(self.instance_norm_d5(h))
        h = self.relu(self.instance_norm_d6(self.d5(h)))
        h = self.relu(self.instance_norm_d7(self.d6(h)))
        h = self.relu(self.instance_norm_d8(self.d7(h)))
        x = self.d8(h)
        x_upsample = F.interpolate(x, size=[self.input_height, self.input_width], mode='bicubic')
        return self.sigmoid(x_upsample)                          
    
    def forward(self, x):
        mu, var = self.encode(x)
        if self.training:
            z = self.reparametrize(mu, var)
        else:
            z = mu
        x_hat = self.decode(z)
        return x_hat, mu, var

reconstruction_function = nn.BCELoss(reduction='sum')
def loss_function(x_hat, x, mu, var):
    BCE = reconstruction_function(x_hat, x)/x_hat.shape[0]
    # https://arxiv.org/abs/1312.6114 (Appendix B)
    # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    logvar = torch.log(var)
    KLD_element = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
    KLD = torch.sum(KLD_element).mul_(-0.5)/x_hat.shape[0] 

    return BCE + KLD, BCE, KLD