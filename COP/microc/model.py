# some of the codes below are copied from Query2label
import torch,math,os
import numpy as np
from torch import nn, Tensor,einsum
from microc.transformer import Transformer
from einops import rearrange
from microc.layers import dilated_tower,AttentionPool
from einops.layers.torch import Rearrange
from hic.model import Tranmodel,build_backbone,build_transformer

# class Tranmodel(nn.Module):
#     def __init__(self, backbone, transfomer, num_class):
#         super().__init__()
#         self.backbone = backbone
#         self.transformer = transfomer
#         self.num_class = num_class
#         hidden_dim = transfomer.d_model
#         self.input_proj = nn.Conv1d(backbone.num_channels, hidden_dim, kernel_size=1)
#     def forward(self, input):
#         input=rearrange(input,'b n c l -> (b n) c l')
#         src = self.backbone(input)
#         src=self.input_proj(src)
#         src = self.transformer(src)
#         return src


class finetunemodel(nn.Module):
    def __init__(self,pretrain_model,hidden_dim,embed_dim,device,trunk,bins=600,in_dim=64,max_bin=100,crop=50,dilate=True):
        super().__init__()
        self.pretrain_model = pretrain_model
        self.bins=bins
        self.max_bin=max_bin
        self.attention_pool = AttentionPool(hidden_dim)
        self.crop=crop
        self.trunk=trunk
        self.dilate=dilate
        self.project=nn.Sequential(
            Rearrange('(b n) c -> b c n', n=bins),
            nn.Conv1d(hidden_dim, hidden_dim, kernel_size=15, padding=7,groups=hidden_dim),
            nn.BatchNorm1d(hidden_dim),
            nn.Conv1d(hidden_dim, embed_dim, kernel_size=1),
            nn.ReLU(inplace=True),
            nn.Dropout(0.2)
        )

        self.cnn=nn.Sequential(
            nn.Conv1d(embed_dim, embed_dim, kernel_size=15, padding=7),
            nn.BatchNorm1d(embed_dim),
            nn.ReLU(inplace=True),
            nn.Conv1d(embed_dim, embed_dim, kernel_size=1),
            nn.Dropout(0.2),
            Rearrange('b c n -> b n c')
        )
        if self.trunk =='LSTM':
            self.BiLSTM=nn.LSTM(input_size=embed_dim, hidden_size=embed_dim//2, num_layers=4,
                                     batch_first=True,
                                     dropout=0.2,
                                     bidirectional=True)
        elif self.trunk =='transformer':
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=embed_dim,
                nhead=4,
                dim_feedforward=2 * embed_dim,
                batch_first=True,
                norm_first=True,
            )
            self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=3)
        else:
            raise ValueError('choose a trunk from LSTM, transformer.')
        self.distance_embed=nn.Embedding(max_bin+1,embed_dim)
        if self.dilate:
            self.dilate_tower=dilated_tower(embed_dim=embed_dim,in_channel=in_dim,kernel_size=7,dilate_rate=6)
            self.prediction_head = nn.Linear(in_dim, 1)
        else:
            self.prediction_head = nn.Linear(embed_dim, 1)
        self.dist_dropout=nn.Dropout(0.1)
        self.device=device

    def position_matrix(self,bins,b,maxbin):
        pos1=np.tile(np.arange(bins),(bins,1))
        pos2=pos1.T
        pos= np.abs(pos1-pos2)
        pos = np.where(pos > maxbin, maxbin, pos)
        pos=np.tile(pos,(b,1,1))
        return torch.tensor(pos).long().to(self.device)
    def upper_tri(self,x,bins):
        triu_tup = np.triu_indices(bins)
        d = np.array(list(triu_tup[1] + bins * triu_tup[0]))
        return x[:, d, :]

    def output_head(self,x,dist_embed,bins):
        x1=torch.tile(x.unsqueeze(1),(1,bins,1,1))
        x2 = x1.permute(0,2,1,3)
        mean_out=(x1+x2)/2
        dot_out=x1*x2
        return mean_out+dot_out+dist_embed

    def forward(self, x):
        b=x.shape[0]
        x=self.pretrain_model(x)
        x = self.attention_pool(x)
        x = self.project(x)
        x=self.cnn(x)
        if self.trunk=='LSTM':
            x, (h_n,h_c)=self.BiLSTM(x)
        elif self.trunk=='transformer':
            x=self.transformer(x)
        # x=self.transformer(x)

        ### shape:(b,200,256)
        dist_embed=self.dist_dropout(self.distance_embed(self.position_matrix(self.bins,b=b,maxbin=self.max_bin)))
        x=self.output_head(x,dist_embed,self.bins)
        if self.dilate:
            x = self.dilate_tower(x,self.crop)
        else:
            x= x[:,self.crop:-self.crop,self.crop:-self.crop,:]
        x=rearrange(x,'b l n d -> b (l n) d')
        x=self.upper_tri(x,self.bins-2*self.crop)
        x=self.prediction_head(x)
        return x


# def build_backbone():
#     model = CNN()
#     return model
# def build_transformer(args):
#     return Transformer(
#         d_model=args.hidden_dim,
#         dropout=args.dropout,
#         nhead=args.nheads,
#         dim_feedforward=args.dim_feedforward,
#         num_encoder_layers=args.enc_layers,
#         num_decoder_layers=args.dec_layers
#     )
def build_pretrain_model_microc(args):
    backbone = build_backbone()
    transformer = build_transformer(args)
    pretrain_model = Tranmodel(
            backbone=backbone,
            transfomer=transformer,
        )
    if args.pretrain_path != 'none':
        print('load pre-training model: '+args.pretrain_path)
        model_dict = pretrain_model.state_dict()
        pretrain_dict = torch.load(args.pretrain_path, map_location='cpu')
        pretrain_dict = {k: v for k, v in pretrain_dict.items() if k in model_dict}
        model_dict.update(pretrain_dict)
        pretrain_model.load_state_dict(model_dict)
    if not args.fine_tune:
        for param in pretrain_model.parameters():
            param.requires_grad = False
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model=finetunemodel(pretrain_model,hidden_dim=args.hidden_dim,embed_dim=args.embed_dim,device=device,bins=args.bins,crop=args.crop,trunk=args.trunk)
    return model

